import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable


class Encoder(nn.Module):
    """Encodes the static & dynamic states using 1d Convolution."""

    def __init__(self, input_size, hidden_size):
        super(Encoder, self).__init__()

        # Use a 1d CNN to embed both the static & dynamic elements
        self.conv = nn.Conv1d(input_size, hidden_size, kernel_size=1)

        for p in self.parameters():
            if len(p.shape) > 1:
                nn.init.xavier_uniform(p)

    def forward(self, input):

        # (batch_size, input_features, seq_len) -> (batch_size, hidden_size, seq_len)
        output = self.conv(input)
        return output


class Attention(nn.Module):
    """Calculates attention over the input nodes given the current state."""

    def __init__(self, hidden_size, use_cuda=False):
        super(Attention, self).__init__()

        # The static, dynamic, and hidden (from decoder) all have the same size
        self.v = nn.Parameter(torch.FloatTensor(1, hidden_size))
        self.W = nn.Parameter(torch.FloatTensor(hidden_size, 3 * hidden_size))

        for p in self.parameters():
            if len(p.shape) > 1:
                nn.init.xavier_uniform(p)

        self.use_cuda = use_cuda

    def forward(self, static_enc, dynamic_enc, decoder_hidden):

        batch_size, _, seq_len = static_enc.size()

        # Attention is calculated across the input sequence, and is a function
        # and is a function of static, dynamic, and output elements
        attns = torch.zeros(batch_size, seq_len)
        if self.use_cuda:
            attns = Variable(attns.cuda())
        else:
            attns = Variable(attns)

        # (batch_size, num_feats, seq_len)
        hidden = decoder_hidden.permute(1, 2, 0).expand(-1, -1, seq_len)

        for i in range(batch_size):

            # ([static, dynamic, output]_feats, seq_len)
            energy = torch.cat((static_enc[i], dynamic_enc[i], hidden[i]), 0)

            attns[i] = torch.mm(self.v, F.tanh(torch.mm(self.W, energy)))

        attns = F.softmax(attns, dim=1)  # (batch, seq_len)
        return attns


class Decoder(nn.Module):
    """Calculates the next state given the previous state and input embeddings."""

    def __init__(self, output_size, hidden_size, dropout=0.2, num_layers=1, use_cuda=False):
        super(Decoder, self).__init__()

        # Use a learnable initial state (x0) & hidden representation (h0), with
        # v & W used to compute the output with attentions
        self.x0 = nn.Parameter(torch.FloatTensor(1, output_size))
        self.h0 = nn.Parameter(torch.FloatTensor(num_layers, 1, hidden_size))
        self.v = nn.Parameter(torch.FloatTensor(1, hidden_size))
        self.W = nn.Parameter(torch.FloatTensor(hidden_size, 2 * hidden_size))

        for p in self.parameters():
            if len(p.shape) > 1:
                nn.init.xavier_uniform(p)

        self.use_cuda = use_cuda

        # Used to compute a representation of the current decoder output
        self.embedding = nn.Linear(output_size, hidden_size)
        self.gru = nn.GRU(hidden_size, hidden_size, num_layers, dropout=dropout)
        self.attn = Attention(hidden_size, use_cuda)

    def forward(self, static_enc, dynamic_enc, last_output, last_hidden):

        batch_size, _, seq_len = static_enc.size()

        # Use a learnable hidden state & input for the decoder
        if last_hidden is None:
            last_hidden = self.h0.expand(-1, batch_size, -1).contiguous()
        if last_output is None:
            last_output = self.x0.expand(batch_size, -1)

        last_embedding = self.embedding(last_output).unsqueeze(0)
        rnn_out, hidden = self.gru(last_embedding, last_hidden)

        # Attention is applied across the static and dynamic states of the input
        attn = self.attn(static_enc, dynamic_enc, rnn_out).unsqueeze(1)

        # The context vector is a weighted combination of the attention + inputs
        context = attn.bmm(static_enc.permute(0, 2, 1))  # (B, 1, num_feats)

        # Calculate the next output using Batch-matrix-multiply ops
        context = context.permute(0, 2, 1).expand(-1, -1, seq_len)
        context = torch.cat((static_enc, context), dim=1)

        W_view = self.W.unsqueeze(0).expand(batch_size, -1, -1)
        v_view = self.v.unsqueeze(0).expand(batch_size, -1, -1)

        outputs = torch.bmm(v_view, F.tanh(torch.bmm(W_view, context)))
        outputs = outputs.squeeze(1)

        return outputs, hidden


class Critic(nn.Module):
    """Estimates the problem complexity."""

    def __init__(self, static_size, dynamic_size, hidden_size, num_process_iter, use_cuda):
        super(Critic, self).__init__()

        self.num_process_iter = num_process_iter
        self.use_cuda = use_cuda

        # Define the encoder & decoder models
        self.static_encoder = Encoder(static_size, hidden_size)
        self.dynamic_encoder = Encoder(dynamic_size, hidden_size)
        self.attn = Attention(hidden_size, use_cuda)
        self.fc1 = nn.Linear(hidden_size, 20)
        self.fc2 = nn.Linear(20, 1)

        for p in self.parameters():
            if len(p.shape) > 1:
                nn.init.xavier_uniform(p)

    def forward(self, static, dynamic, initial_state):

        static_enc = self.static_encoder(static)
        dynamic_enc = self.dynamic_encoder(dynamic)

        if initial_state is None:
            # Use a zero'd context vector
            context = torch.zeros(1, static_enc.size(0), static_enc.size(1))
            context = Variable(context.cuda() if self.use_cuda else context)
        else:
            # Pass initial state through an encoder
            context = self.static_encoder(initial_state.unsqueeze(2)).permute(2, 0, 1)

        for _ in range(self.num_process_iter):

            # Attention is applied across the static and dynamic states
            attn = self.attn(static_enc, dynamic_enc, context).unsqueeze(1)
            context[0] = attn.bmm(static_enc.permute(0, 2, 1)).squeeze(1)

        output = F.relu(self.fc1(context.squeeze(0)))
        output = self.fc2(output)

        return output


class DRL4VRP(nn.Module):

    def __init__(self, static_size, dynamic_size, hidden_size, update_fn=None,
                 mask_fn=None, dropout=0., num_layers=1, use_cuda=False):
        super(DRL4VRP, self).__init__()

        self.update_fn = update_fn
        self.mask_fn = mask_fn
        self.use_cuda = use_cuda

        # Define the encoder & decoder models
        self.static_encoder = Encoder(static_size, hidden_size)
        self.dynamic_encoder = Encoder(dynamic_size, hidden_size)
        self.decoder = Decoder(static_size, hidden_size, dropout, num_layers, use_cuda)

        if use_cuda:
            self.cuda()

    def forward(self, static, dynamic, last_output=None, last_hidden=None):
        """

        If we've supplied a masking function (on construction), we'll use that 
        to determine when to stop (and use an arbitrary number of iters). 
        Otherwise, treat the problem as a TSP, and only perform seq_len iterations

        Parameters
        ----------
        mask_fn:
            We can speed up learning by preventing states from being selected
            by forcing a prob of -inf.
        """

        # Structures for holding the output sequences
        decoder_probs, decoder_indices = [], []

        max_iters = static.size(2) if self.mask_fn is None else 1000

        mask = torch.ones(static.size(0), static.size(2))
        mask = Variable(mask.cuda() if self.use_cuda else mask)

        static_enc = self.static_encoder(static)
        dynamic_enc = self.dynamic_encoder(dynamic)

        for _ in range(max_iters):

            probs, last_hidden = self.decoder(static_enc, dynamic_enc,
                                              last_output, last_hidden)

            # Use mask.log() to prevent certain indices from being selected. From:
            # https://github.com/pemami4911/neural-combinatorial-rl-pytorch/issues/5
            probs = F.softmax(probs + mask.log(), dim=1)

            # When training we sample the next state, but for testing use greedy
            if self.training:
                ptr = torch.distributions.Categorical(probs).sample()
            else:
                ptr = torch.max(probs, 1)[1]

            view = ptr.view(-1, 1, 1).expand(-1, static.size(1), -1)
            last_output = torch.gather(static, 2, view).clone().squeeze(2)

            # Keep track of the probability we used in selecting the action
            top_prob = probs[np.arange(static.size(0)), ptr.data]
            decoder_probs.append(top_prob.unsqueeze(1))
            decoder_indices.append(ptr.data.unsqueeze(1))

            # Update the dynamics variables
            if self.update_fn is not None:
                dynamic = self.update_fn(dynamic, ptr.data)
                dynamic_enc = self.dynamic_encoder(dynamic)

            # Update the mask
            if self.mask_fn is not None:
                mask = self.mask_fn(mask.clone(), dynamic, ptr.data)
                if not mask.byte().any():
                    break

        # (batch_size, seq_len)
        decoder_probs = torch.cat(decoder_probs, dim=1)
        decoder_indices = torch.cat(decoder_indices, dim=1)
        return decoder_indices, decoder_probs


if __name__ == '__main__':
    raise Exception('Cannot be called from main')
