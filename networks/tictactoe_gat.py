import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader, TensorDataset, DistributedSampler
from sklearn.model_selection import train_test_split
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.amp import autocast, GradScaler
import numpy as np
import os

class GATLayer(nn.Module):
    def __init__(self, in_features, out_features, num_heads, concat=True, activation=nn.ELU(),
                 dropout_prob=0.6, add_skip_connection=True, bias=True):
        super(GATLayer, self).__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.num_heads = num_heads
        self.concat = concat
        self.activation = activation
        self.add_skip_connection = add_skip_connection
        
        self.linear_proj = nn.Linear(in_features, num_heads * out_features, bias=False)
        self.scoring_fn_target = nn.Parameter(torch.Tensor(1, num_heads, out_features))
        self.scoring_fn_source = nn.Parameter(torch.Tensor(1, num_heads, out_features))
        
        if bias and concat:
            self.bias = nn.Parameter(torch.Tensor(num_heads * out_features))
        elif bias and not concat:
            self.bias = nn.Parameter(torch.Tensor(out_features))
        else:
            self.register_parameter('bias', None)
        
        if add_skip_connection:
            self.skip_proj = nn.Linear(in_features, num_heads * out_features, bias=False)
        else:
            self.register_parameter('skip_proj', None)
        
        self.leakyReLU = nn.LeakyReLU(0.2)
        self.softmax = nn.Softmax(dim=-1)
        self.dropout = nn.Dropout(dropout_prob)
        
        self.init_params()

    def init_params(self):
        nn.init.xavier_uniform_(self.linear_proj.weight)
        nn.init.xavier_uniform_(self.scoring_fn_source)
        nn.init.xavier_uniform_(self.scoring_fn_target)
        if self.bias is not None:
            torch.nn.init.zeros_(self.bias)

    def forward(self, x, edge_index):
        num_nodes = x.size(0)

        # Linear projection and regularization
        x = self.dropout(x)
        x = self.linear_proj(x).view(-1, self.num_heads, self.out_features)
        x = self.dropout(x)

        # Edge attention calculation
        scores_source = (x * self.scoring_fn_source).sum(dim=-1)
        scores_target = (x * self.scoring_fn_target).sum(dim=-1)
        scores_source_lifted, scores_target_lifted, x_lifted = self.lift(scores_source, scores_target, x, edge_index)
        scores_per_edge = self.leakyReLU(scores_source_lifted + scores_target_lifted)
        
        attentions_per_edge = self.neighborhood_aware_softmax(scores_per_edge, edge_index[1], num_nodes)
        attentions_per_edge = self.dropout(attentions_per_edge)

        # Neighborhood aggregation
        x_lifted_weighted = x_lifted * attentions_per_edge
        out_nodes_features = self.aggregate_neighbors(x_lifted_weighted, edge_index, num_nodes)

        # Skip connection and bias
        out_nodes_features = self.skip_concat_bias(x, out_nodes_features)

        return out_nodes_features if self.activation is None else self.activation(out_nodes_features)

    def lift(self, scores_source, scores_target, x, edge_index):
        src_nodes_index = edge_index[0]
        trg_nodes_index = edge_index[1]
        scores_source = scores_source.index_select(0, src_nodes_index)
        scores_target = scores_target.index_select(0, trg_nodes_index)
        x_lifted = x.index_select(0, src_nodes_index)
        return scores_source, scores_target, x_lifted

    def neighborhood_aware_softmax(self, scores_per_edge, trg_index, num_of_nodes):
        scores_per_edge = scores_per_edge - scores_per_edge.max()
        exp_scores_per_edge = scores_per_edge.exp()
        neigborhood_aware_denominator = self.sum_edge_scores_neighborhood_aware(exp_scores_per_edge, trg_index, num_of_nodes)
        attentions_per_edge = exp_scores_per_edge / (neigborhood_aware_denominator + 1e-16)
        return attentions_per_edge.unsqueeze(-1)

    def sum_edge_scores_neighborhood_aware(self, exp_scores_per_edge, trg_index, num_of_nodes):
        trg_index_broadcasted = self.explicit_broadcast(trg_index, exp_scores_per_edge)
        size = list(exp_scores_per_edge.shape)
        size[0] = num_of_nodes
        neighborhood_sums = torch.zeros(size, dtype=exp_scores_per_edge.dtype, device=exp_scores_per_edge.device)
        neighborhood_sums.scatter_add_(0, trg_index_broadcasted, exp_scores_per_edge)
        return neighborhood_sums.index_select(0, trg_index)

    def aggregate_neighbors(self, x_lifted_weighted, edge_index, num_of_nodes):
        trg_index_broadcasted = self.explicit_broadcast(edge_index[1], x_lifted_weighted)
        size = list(x_lifted_weighted.shape)
        size[0] = num_of_nodes
        out_nodes_features = torch.zeros(size, dtype=x_lifted_weighted.dtype, device=x_lifted_weighted.device)
        out_nodes_features.scatter_add_(0, trg_index_broadcasted, x_lifted_weighted)
        return out_nodes_features

    def skip_concat_bias(self, x, out_nodes_features):
        if self.add_skip_connection:
            if out_nodes_features.shape[-1] == x.shape[-1]:
                out_nodes_features += x.unsqueeze(1)
            else:
                out_nodes_features += self.skip_proj(x).view(-1, self.num_heads, self.out_features)

        if self.concat:
            out_nodes_features = out_nodes_features.view(-1, self.num_heads * self.out_features)
        else:
            out_nodes_features = out_nodes_features.mean(dim=1)

        if self.bias is not None:
            out_nodes_features += self.bias

        return out_nodes_features

    def explicit_broadcast(self, this, other):
        for _ in range(this.dim(), other.dim()):
            this = this.unsqueeze(-1)
        return this.expand_as(other)

class TicTacToeGAT(nn.Module):
    def __init__(self, game, args):
        super(TicTacToeGAT, self).__init__()
        self.board_x, self.board_y = game.get_board_size()
        self.action_size = game.get_action_size()
        self.args = args

        self.num_nodes = self.board_x * self.board_y
        self.num_features = 3  # empty, X, O

        self.gat1 = GATLayer(self.num_features, args.num_channels, num_heads=4, dropout_prob=0.3)
        self.gat2 = GATLayer(args.num_channels * 4, args.num_channels, num_heads=4, dropout_prob=0.3)
        
        self.fc1 = nn.Linear(args.num_channels * 4 * self.num_nodes, 256)
        self.fc2 = nn.Linear(256, 128)
        
        self.fc_policy = nn.Linear(128, self.action_size)
        self.fc_value = nn.Linear(128, 1)

    def forward(self, s):
        x, edge_index = self._board_to_graph(s)
        
        x = self.gat1(x, edge_index)
        x = F.elu(x)
        x = self.gat2(x, edge_index)
        x = F.elu(x)

        x = x.view(-1, self.args.num_channels * 4 * self.num_nodes)
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))

        pi = self.fc_policy(x)
        v = self.fc_value(x)

        return F.log_softmax(pi, dim=1), torch.tanh(v)

    def _board_to_graph(self, s):
        # Ensure s is a 4D tensor (batch_size, channels, height, width)
        if s.dim() == 3:
            s = s.unsqueeze(1)
        elif s.dim() == 2:
            s = s.unsqueeze(0).unsqueeze(0)
        
        batch_size, channels, height, width = s.shape
        
        # Reshape s to (batch_size * num_nodes, channels)
        s_flat = s.view(batch_size, channels, -1).transpose(1, 2).contiguous().view(-1, channels)
        
        x = torch.zeros(batch_size * self.num_nodes, 3, device=s.device)
        x[:, 0] = (s_flat == 0).float().sum(dim=1)
        x[:, 1] = (s_flat == 1).float().sum(dim=1)
        x[:, 2] = (s_flat == -1).float().sum(dim=1)
        
        # Create fully connected edge index for a single graph
        edge_index_single = torch.combinations(torch.arange(self.num_nodes, device=s.device), r=2).t()
        edge_index_single = torch.cat([edge_index_single, edge_index_single.flip(0)], dim=1)
        
        # Repeat the edge index for each graph in the batch
        edge_index = edge_index_single.repeat(1, batch_size)
        batch_offset = torch.arange(batch_size, device=s.device).repeat_interleave(edge_index_single.size(1)) * self.num_nodes
        edge_index = edge_index + batch_offset
        
        return x, edge_index

class NNetWrapper:
    def __init__(self, game, args):
        self.game = game
        self.args = args
        self.board_x, self.board_y = game.get_board_size()
        self.action_size = game.get_action_size()

        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.nnet = TicTacToeGAT(game, args).to(self.device)

        if args.distributed:
            self.nnet = DDP(self.nnet, device_ids=[args.local_rank], output_device=args.local_rank)
        elif torch.cuda.device_count() > 1:
            self.nnet = nn.DataParallel(self.nnet)

        self.optimizer = optim.Adam(self.nnet.parameters(), lr=args.lr, weight_decay=args.l2_regularization)
        self.scheduler = ReduceLROnPlateau(self.optimizer, 'min', patience=5, factor=0.5)
        self.scaler = GradScaler()
        self.criterion_pi = nn.CrossEntropyLoss()
        self.criterion_v = nn.MSELoss()

    def train(self, examples):
        train_examples, val_examples = train_test_split(examples, test_size=0.2)

        train_data = TensorDataset(
            torch.FloatTensor([ex[0] for ex in train_examples]),
            torch.FloatTensor([ex[1] for ex in train_examples]),
            torch.FloatTensor([ex[2] for ex in train_examples])
        )
        
        train_loader = DataLoader(train_data, batch_size=self.args.batch_size, shuffle=True)

        for epoch in range(self.args.epochs):
            self.nnet.train()
            total_loss = 0
            for batch_idx, (boards, target_pis, target_vs) in enumerate(train_loader):
                boards, target_pis, target_vs = boards.to(self.device), target_pis.to(self.device), target_vs.to(self.device)
                
                self.optimizer.zero_grad()
                
                with autocast(device_type=self.device.type):
                    out_pi, out_v = self.nnet(boards)
                    l_pi = self.criterion_pi(out_pi, target_pis)
                    l_v = self.criterion_v(out_v.squeeze(-1), target_vs)
                    loss = l_pi + l_v

                self.scaler.scale(loss).backward()
                self.scaler.step(self.optimizer)
                self.scaler.update()

                total_loss += loss.item()

            val_loss = self.validate(val_examples)
            self.scheduler.step(val_loss)

            print(f'Epoch {epoch+1}/{self.args.epochs}, Train Loss: {total_loss/len(train_loader):.3f}, Val Loss: {val_loss:.3f}')

    def validate(self, val_examples):
        self.nnet.eval()
        val_loss = 0
        with torch.no_grad():
            for board, target_pi, target_v in val_examples:
                board = torch.FloatTensor(board.astype(np.float64)).unsqueeze(0).to(self.device)
                target_pi = torch.FloatTensor(target_pi).unsqueeze(0).to(self.device)
                target_v = torch.FloatTensor([target_v]).to(self.device)
                
                out_pi, out_v = self.nnet(board)
                l_pi = self.criterion_pi(out_pi, target_pi)
                l_v = self.criterion_v(out_v.squeeze(-1), target_v)
                val_loss += (l_pi + l_v).item()

        return val_loss / len(val_examples)

    def predict(self, board):
        board = torch.FloatTensor(board.astype(np.float64))
        board = board.unsqueeze(0).unsqueeze(0).to(self.device)
        
        self.nnet.eval()
        with torch.no_grad():
            pi, v = self.nnet(board)

        return pi.exp().cpu().numpy()[0], v.cpu().numpy()[0]

    def save_checkpoint(self, folder='checkpoint', filename='checkpoint.pth.tar'):
        filepath = os.path.join(folder, filename)
        if not os.path.exists(folder):
            os.makedirs(folder)

        torch.save({
            'state_dict': self.nnet.state_dict(),
            'optimizer': self.optimizer.state_dict(),
            'scheduler': self.scheduler.state_dict(),
            'scaler': self.scaler.state_dict(),
        }, filepath)

    def load_checkpoint(self, folder='checkpoint', filename='checkpoint.pth.tar'):
        filepath = os.path.join(folder, filename)
        if not os.path.exists(filepath):
            raise ValueError(f"No model in path '{filepath}'")

        checkpoint = torch.load(filepath, map_location=self.device)

        self.nnet.load_state_dict(checkpoint['state_dict'])
        self.optimizer.load_state_dict(checkpoint['optimizer'])
        self.scheduler.load_state_dict(checkpoint['scheduler'])
        self.scaler.load_state_dict(checkpoint['scaler'])

    def augment_examples(self, examples):
        augmented = []
        for board, pi, v in examples:
            augmented.append((board, pi, v))
            
            for k in range(1, 4):
                rotated_board = np.rot90(board, k)
                rotated_pi = np.zeros_like(pi)
                rotated_pi[:9] = np.rot90(pi[:9].reshape(3, 3), k).flatten()
                rotated_pi[9] = pi[9]
                augmented.append((rotated_board, rotated_pi, v))
            
            flipped_board = np.fliplr(board)
            flipped_pi = np.zeros_like(pi)
            flipped_pi[:9] = np.fliplr(pi[:9].reshape(3, 3)).flatten()
            flipped_pi[9] = pi[9]
            augmented.append((flipped_board, flipped_pi, v))
            
        return augmented