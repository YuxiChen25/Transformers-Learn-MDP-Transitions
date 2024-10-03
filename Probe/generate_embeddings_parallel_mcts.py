from model import Config, GPTModel
import torch.nn as nn
import torch
from accelerate import Accelerator
from accelerate.utils import load_checkpoint_and_dispatch
import argparse
import os
import pickle
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torch.nn.utils.rnn import pad_sequence

accelerator = Accelerator()

class Config:
    """
    Configuration for the GPT model including size parameters and dropout rates.
    """
    def __init__(self, vocab_size, block_size, n_embd, n_head, n_layer):
        self.vocab_size = vocab_size  # Vocabulary size
        self.block_size = block_size  # Input sequence length
        self.n_embd = n_embd          # Embedding dimension
        self.n_head = n_head          # Number of attention heads
        self.n_layer = n_layer        # Number of transformer layers
        # Dropout rates
        self.embd_pdrop = 0.1
        self.resid_pdrop = 0.1
        self.attn_pdrop = 0.1

class CausalSelfAttention(nn.Module):
    """
    Causal self-attention module implementing scaled dot-product attention.
    """
    def __init__(self, config):
        super().__init__()
        self.config = config
        assert self.config.n_embd % self.config.n_head == 0, "embedding dimension must be divisible by the number of heads."

        # Key, query, and value linear transformations
        self.key = nn.Linear(self.config.n_embd, self.config.n_embd)
        self.query = nn.Linear(self.config.n_embd, self.config.n_embd)
        self.value = nn.Linear(self.config.n_embd, self.config.n_embd)

        # Dropout layers
        self.attn_drop = nn.Dropout(self.config.attn_pdrop)
        self.resid_drop = nn.Dropout(self.config.resid_pdrop)

        # Output projection
        self.proj = nn.Linear(self.config.n_embd, self.config.n_embd)

        # Causal mask to prevent attention to future tokens
        self.register_buffer("mask", torch.tril(torch.ones(config.block_size, config.block_size)).unsqueeze(0).unsqueeze(1))

    def forward(self, x):
        B, T, C = x.size()  # Batch size, sequence length, embedding dimension

        # Calculate projections and reshape for multi-headed attention
        k = self.key(x).view(B, T, self.config.n_head, C // self.config.n_head).transpose(1, 2)
        q = self.query(x).view(B, T, self.config.n_head, C // self.config.n_head).transpose(1, 2)
        v = self.value(x).view(B, T, self.config.n_head, C // self.config.n_head).transpose(1, 2)

        # Compute the attention scores
        att = (q @ k.transpose(-2, -1)) * (1.0 / math.sqrt(k.size(-1)))
        att = att.masked_fill(self.mask[:,:,:T,:T] == 0, float('-inf'))
        att = F.softmax(att, dim=-1)
        att = self.attn_drop(att) @ v

        # Re-assemble all head outputs side by side
        y = att.transpose(1, 2).contiguous().view(B, T, C)
        
        # Output projection
        y = self.resid_drop(self.proj(y))
        return y

class TransformerBlock(nn.Module):
    """
    A single transformer block containing a causal self-attention layer and a feed-forward network.
    """
    def __init__(self, config):
        super().__init__()
        self.attn = CausalSelfAttention(config)
        self.ln1 = nn.LayerNorm(config.n_embd)
        self.mlp = nn.Sequential(
            nn.Linear(config.n_embd, 4 * config.n_embd),
            nn.GELU(),
            nn.Linear(4 * config.n_embd, config.n_embd),
            nn.Dropout(config.resid_pdrop),
        )
        self.ln2 = nn.LayerNorm(config.n_embd)

    def forward(self, x):
        x = x + self.attn(self.ln1(x))
        x = x + self.mlp(self.ln2(x))
        return x

class GPTModel(nn.Module):
    """
    The GPT model comprising an embedding layer, multiple transformer blocks, and a final output layer.
    """
    def __init__(self, config):
        super().__init__()
        self.config = config
        
        # Embedding layers
        self.tok_emb = nn.Embedding(self.config.vocab_size, self.config.n_embd)
        self.pos_emb = nn.Parameter(torch.zeros(1, self.config.block_size, self.config.n_embd))
        self.drop = nn.Dropout(self.config.embd_pdrop)
        
        # Transformer blocks
        self.blocks = nn.Sequential(*[TransformerBlock(config) for _ in range(self.config.n_layer)])
        
        # Final layer normalization and linear output layer
        self.ln_f = nn.LayerNorm(self.config.n_embd)
        self.head = nn.Linear(self.config.n_embd, self.config.vocab_size, bias=False)
        
        self.apply(self._init_weights)
    
    def _init_weights(self, module):
        # Initialize weights and biases
        if isinstance(module, (nn.Linear, nn.Embedding)):
            module.weight.data.normal_(mean=0.0, std=0.02)
            if isinstance(module, nn.Linear) and module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)

    def forward(self, idx, return_embeddings=False):
        # Processing input
        _, T = idx.size()
        assert T <= self.config.block_size, "Input sequence too long."
        
        token_embeddings = self.tok_emb(idx)
        position_embeddings = self.pos_emb[:, :T, :]
        x = self.drop(token_embeddings + position_embeddings)

        # Retrieve Embedding if num_probe is specified
        if return_embeddings:
            embeddings = []
            for block in self.blocks:
                x = block(x)
                embeddings.append(x.copy())
            return embeddings
                
        # Process through all blocks, then project to vocabulary size
        x = self.blocks(x)
        x = self.ln_f(x)
        logits = self.head(x)
        
        return logits

def collate_fn(batch):

    Xs, Ys = zip(*batch)

    Xs_padded = pad_sequence(Xs, batch_first=True, padding_value=0)
    Ys_padded = pad_sequence(Ys, batch_first=True, padding_value=0)

    return Xs_padded, Ys_padded

def generate_embeddings(dataset, config, model_load_path):

    accelerator = Accelerator()
    
    model = load_checkpoint_and_dispatch(
        GPTModel(config),
        model_load_path,
        device_map="auto"
    )

    dataset_loader = DataLoader(dataset, batch_size=1, shuffle=False, collate_fn = collate_fn)

    model, dataset_loader = accelerator.prepare(model, dataset_loader)

    model.eval()

    embeddings_qvalues = []

    with torch.no_grad():

        # x is the game up until time t
        for x, qvalues in dataset_loader:

            rng = torch.Generator
            rng.manual_seed(x.size(1))
            idx = torch.randperm(x.size(1), generator = rng)[0]

            list_of_embeddings = model(x[idx], generate_embeddings=True)
            embeddings_qvalues.append((list_of_embeddings, qvalues[idx]))

    accelerator.wait_for_everyone()

    all_embeddings_qvalues = accelerator.gather(embeddings_qvalues)
    return all_embeddings_qvalues


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-m', type=int, default=0, choices=[0, 1, 2], help='Data Mode (state, action, state-action)')
    parser.add_argument('-s', type=int, default=0, choices=[0, 1, 2], help='Seed')
    parser.add_argument('-e', type=int, help='Epoch')
    args = parser.parse_args()
    if args.m == 0:
        token_to_idx = {(i, j): i * 7 + j + 1 for i in range(6) for j in range(7)}
        vocab_size = 43
    elif args.m == 1:
        token_to_idx = {i: i + 1 for i in range(7)}
        vocab_size = 8
    elif args.m == 2:
        token_to_idx = {(i, j): i * 7 + j + 1 for i in range(6) for j in range(7)} | {i: i + 44 for i in range(7)}
        vocab_size = 51
    
    token_to_idx['<pad>'] = 0 
    block_size = 42 
    embed_size = 512
    num_layers = 8
    config = Config(vocab_size, block_size, n_layer=num_layers, n_head=num_layers, n_embd=embed_size)

    path = ''
    model_load_path = os.path.join(path, rf'mcts_mode{args.m}/best_model/model_{args.e}_mode_{args.m}_seed_{args.s}.pth')

    with open(os.path.join(path, rf'training_data/mcts/mcts_vals_mode_{args.m}.pkl'), 'rb') as f:
        qagent1 = pickle.load(f)

    train_ratio = 0.8
    valid_ratio = 0.1

    q1 = len(qagent1)

    qtrain = qagent1[:int(train_ratio * q1)]
    qvalid = qagent1[int(train_ratio * q1):int((train_ratio + valid_ratio) * q1)]
    qtest = qagent1[int((train_ratio + valid_ratio) * q1):]

    embeddings = generate_embeddings(qtrain, config, model_load_path)

    with open(os.path.join(path, rf'embeddings_mcts_mode_{args.m}_seed_{args.s}.pkl'), 'rb') as f:
        pickle.dump(embeddings, f)