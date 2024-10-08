import logging
import os
import sys
from collections import deque
from pickle import Pickler, Unpickler
from random import shuffle

import numpy as np
import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm

from arena import Arena
from mcts import MCTS

log = logging.getLogger(__name__)

class SelfPlay():
    def __init__(self, game, nnet, args):
        self.game = game
        self.args = args
        self.nnet = nnet
        
        if args.distributed:
            self.device = torch.device(f"cuda:{args.local_rank}")
            self.world_size = dist.get_world_size()
            self.rank = dist.get_rank()
        else:
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            self.world_size = 1
            self.rank = 0

        self.pnet = self.nnet.__class__(self.game, self.args)
        if args.distributed:
            self.pnet.setup_distributed(args)
        
        self.mcts = MCTS(self.game, self.nnet, self.args)
        self.trainExamplesHistory = []
        self.skipFirstSelfPlay = False

    def executeEpisode(self):
        trainExamples = []
        board = self.game.get_init_board()
        self.curPlayer = 1
        episodeStep = 0

        while True:
            episodeStep += 1
            canonicalBoard = self.game.get_canonical_form(board, self.curPlayer)
            temp = int(episodeStep < self.args.tempThreshold)

            # Ensure canonicalBoard is batched
            canonicalBoard = canonicalBoard.unsqueeze(0)

            pi = self.mcts.get_action_prob(canonicalBoard, temp=temp)
            
            sym = self.game.get_symmetries(canonicalBoard.squeeze(0), pi)
            for b, p in sym:
                trainExamples.append([b, self.curPlayer, p, None])

            action = torch.multinomial(torch.from_numpy(pi), 1).item()
            board, self.curPlayer = self.game.get_next_state(board, self.curPlayer, action)

            r = self.game.get_game_ended(board, self.curPlayer)

            if r != 0:
                return [(x[0], x[2], r * ((-1) ** (x[1] != self.curPlayer))) for x in trainExamples]

    def learn(self):
        for i in range(1, self.args.numIters + 1):
            if self.rank == 0:
                log.info(f'Starting Iter #{i} ...')
            
            if not self.skipFirstSelfPlay or i > 1:
                iterationTrainExamples = deque([], maxlen=self.args.maxlenOfQueue)

                episode_num = self.args.numEps // self.world_size
                for _ in tqdm(range(episode_num), desc="Self Play", disable=self.rank != 0):
                    self.mcts = MCTS(self.game, self.nnet, self.args)
                    iterationTrainExamples += self.executeEpisode()

                # Gather examples from all processes
                if self.args.distributed:
                    all_examples = [None for _ in range(self.world_size)]
                    dist.all_gather_object(all_examples, iterationTrainExamples)
                    
                    if self.rank == 0:
                        iterationTrainExamples = deque(sum(all_examples, []), maxlen=self.args.maxlenOfQueue)
                
                if self.rank == 0:
                    self.trainExamplesHistory.append(iterationTrainExamples)

                    if len(self.trainExamplesHistory) > self.args.numItersForTrainExamplesHistory:
                        log.warning(f"Removing the oldest entry in trainExamples. len(trainExamplesHistory) = {len(self.trainExamplesHistory)}")
                        self.trainExamplesHistory.pop(0)
                    
                    self.saveTrainExamples(i - 1)

            if self.rank == 0:
                # shuffle examples before training
                trainExamples = []
                for e in self.trainExamplesHistory:
                    trainExamples.extend(e)
                shuffle(trainExamples)

            # Broadcast trainExamples to all processes
            if self.args.distributed:
                if self.rank == 0:
                    examples_tensor = torch.tensor(trainExamples, dtype=torch.float32)
                else:
                    examples_tensor = torch.empty((0,), dtype=torch.float32)
                
                # Broadcast the size first
                size_tensor = torch.tensor([examples_tensor.shape[0]], dtype=torch.long)
                dist.broadcast(size_tensor, src=0)
                
                if self.rank != 0:
                    examples_tensor = torch.empty(size_tensor[0].item(), dtype=torch.float32)
                
                # Then broadcast the data
                dist.broadcast(examples_tensor, src=0)
                
                trainExamples = examples_tensor.tolist()

            # training new network, keeping a copy of the old one
            self.nnet.save_checkpoint(folder=self.args.checkpoint, filename='temp.pth.tar')
            self.pnet.load_checkpoint(folder=self.args.checkpoint, filename='temp.pth.tar')
            pmcts = MCTS(self.game, self.pnet, self.args)

            self.nnet.train(trainExamples)

            nmcts = MCTS(self.game, self.nnet, self.args)

            if self.rank == 0:
                log.info('PITTING AGAINST PREVIOUS VERSION')
                arena = Arena(lambda x: np.argmax(pmcts.get_action_prob(x, temp=0)),  # Changed from getActionProb to get_action_prob
                              lambda x: np.argmax(nmcts.get_action_prob(x, temp=0)),  # Changed from getActionProb to get_action_prob
                              self.game)
                pwins, nwins, draws = arena.playGames(self.args.arenaCompare)

                log.info('NEW/PREV WINS : %d / %d ; DRAWS : %d' % (nwins, pwins, draws))
                if pwins + nwins == 0 or float(nwins) / (pwins + nwins) < self.args.updateThreshold:
                    log.info('REJECTING NEW MODEL')
                    self.nnet.load_checkpoint(folder=self.args.checkpoint, filename='temp.pth.tar')
                else:
                    log.info('ACCEPTING NEW MODEL')
                    self.nnet.save_checkpoint(folder=self.args.checkpoint, filename=self.getCheckpointFile(i))
                    self.nnet.save_checkpoint(folder=self.args.checkpoint, filename='best.pth.tar')

            if self.args.distributed:
                dist.barrier()

    def getCheckpointFile(self, iteration):
        return 'checkpoint_' + str(iteration) + '.pth.tar'

    def saveTrainExamples(self, iteration):
        if self.rank == 0:
            folder = self.args.checkpoint
            if not os.path.exists(folder):
                os.makedirs(folder)
            filename = os.path.join(folder, self.getCheckpointFile(iteration) + ".examples")
            with open(filename, "wb+") as f:
                Pickler(f).dump(self.trainExamplesHistory)

    def loadTrainExamples(self):
        if self.rank == 0:
            modelFile = os.path.join(self.args.load_folder_file[0], self.args.load_folder_file[1])
            examplesFile = modelFile + ".examples"
            if not os.path.isfile(examplesFile):
                log.warning(f'File "{examplesFile}" with trainExamples not found!')
                r = input("Continue? [y|n]")
                if r != "y":
                    sys.exit()
            else:
                log.info("File with trainExamples found. Loading it...")
                with open(examplesFile, "rb") as f:
                    self.trainExamplesHistory = Unpickler(f).load()
                log.info('Loading done!')

                # examples based on the model were already collected (loaded)
                self.skipFirstSelfPlay = True

        if self.args.distributed:
            dist.barrier()