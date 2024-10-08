from collections import defaultdict
import math
from dqn import DQNAgent, get_valid_locations
import os
import torch
import numpy as np
from torch.nn import MSELoss, KLDivLoss

def load_or_initialize_model(model_path, output_channels, criterion):
    model = DQNAgent(output_channels, criterion)
    if os.path.exists(model_path):
        model.dqn.load_state_dict(torch.load(model_path))
    model.prepare()
    return model

class MCTS:

    def __init__(self, alpha=1):

        # total reward for each node
        self.Q = defaultdict(float)  

        # total visits for each node
        self.N = defaultdict(int)  

        # probability of visiting each node (as per alphazero PUCT)
        self.Pr = defaultdict(float)

        # children of each node
        self.children = dict()  

        # Trade-Off Between Exploration/Exploitation
        self.alpha = alpha

        self.policy_network = load_or_initialize_model('policy_network.pth', 7, KLDivLoss(reduction='batchmean'))

        self.value_network = load_or_initialize_model('value_network.pth', 1, MSELoss())

        self.P = dict()

        
    def choose(self, node):

        if node.is_terminal():
            raise RuntimeError(f"Terminal Node: {node}")

        if node not in self.children:
            return node.find_random_child()

        monte_carlo_candidates = []
        for child in self.children[node]:
            if self.N[child] == 0:
                child_score = float("-inf")
            else:
                child_score = self.Q[child]
            move_representation = getattr(child, 'last_move', 'Unknown move')
            monte_carlo_candidates.append([move_representation, child_score])

        best_child = max(self.children[node], key=lambda n: float("-inf") if self.N[n] == 0 else self.Q[n])
        
        return (best_child, monte_carlo_candidates)

    def rollout(self, node):
        
        # Rollout For 1 Iteration

        path = self.select(node)
        leaf = path[-1]
        p, v = self.expand(leaf, path)
        self.backpropagate(path, v)

    def select(self, node):

        # Find An Unexplored Descendant

        path = []
        while True:
            path.append(node)

            # Returns Unexplored or Terminal Node
            if node not in self.children or not self.children[node]:
                return path
            
            # Finds Unexplored Nodes, if any
            unexplored = self.children[node] - self.children.keys()

            if unexplored:
                chosen = unexplored.pop()
                path.append(chosen)
                return path
            
            node = self.UCT(node)  

    def expand(self, node, path):
        
        three_moves = []
        for i in range(len(path) - 1, max(-1, len(path) - 4), -1):
            three_moves.insert(0, path[i].board)
        while len(three_moves) < 3:
            three_moves.insert(0, [[0 for _ in range(7)] for _ in range(6)])
        obs_tensor = torch.tensor(three_moves, dtype=torch.float32)

        with torch.no_grad():
            policy = self.policy_network.dqn(obs_tensor).cpu().numpy().flatten()
            value = self.value_network.dqn(obs_tensor).item()

        #pr vector

        valid_moves = [x[1] for x in get_valid_locations(node.board)]
        for i in range(7):
            if i not in valid_moves:
                policy[i] = 0

        if sum(policy) > 0:
            policy /= sum(policy)

        self.P[node] = policy
        # self.Q[node] = value

        # Already Contained in Dictionary
        if node in self.children or node.is_terminal():
            return policy, value

        # Update Dictionary
        self.children[node] = node.find_children()
        for i in range(len(valid_moves)):
            self.Pr[self.children[node][i]] = policy[i]

        return policy, value


    def backpropagate(self, path, v):
        for node in reversed(path):
            # v = -v  
            self.N[node] += 1
            self.Q[node] = ((self.N[node] - 1) * self.Q[node] + v)/ self.N[node]
            v = -v

    def UCT(self, node):
        
        # UCT Selection
        # All children Should Be Expanded
        assert all(n in self.children for n in self.children[node])

        for idx, child in enumerate(self.children[node]):
            highest_uct = float('-inf')
            uct = self.Q[child] + self.Pr[child] * (
                    math.sqrt(self.N[node]) / (1 + self.N[child]))
            if uct > highest_uct:
                highest_uct = uct
                highest_index = idx

        return self.children[node][highest_index]

    def reset(self):
        self.Q = defaultdict(float)  

        self.N = defaultdict(int)  

        self.Pr = defaultdict(float)

        self.children = dict()  

        self.P = dict()


