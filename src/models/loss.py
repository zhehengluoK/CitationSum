"""
This file handles the details of the loss function during training.

This includes: LossComputeBase and the standard NMTLossCompute, and
               sharded loss compute stuff.
"""
from __future__ import division
import torch
import GPUtil
import wandb
import torch.nn as nn
import torch.nn.functional as F

from models.reporter import Statistics
from fastNLP.core import seq_len_to_mask

def abs_loss(generator, symbols, vocab_size, device, train=True, label_smoothing=0.0):
    compute = NMTLossCompute(
        generator, symbols, vocab_size,
        label_smoothing=label_smoothing if train else 0.0)
    compute.to(device)
    return compute


class LossComputeBase(nn.Module):
    """
    Class for managing efficient loss computation. Handles
    sharding next step predictions and accumulating mutiple
    loss computations


    Users can implement their own loss computation strategy by making
    subclass of this one.  Users need to implement the _compute_loss()
    and make_shard_state() methods.

    Args:
        generator (:obj:`nn.Module`) :
             module that maps the output of the decoder to a
             distribution over the target vocabulary.
        tgt_vocab (:obj:`Vocab`) :
             torchtext vocab object representing the target output
        normalzation (str): normalize by "sents" or "tokens"
    """

    def __init__(self, generator, pad_id):
        super(LossComputeBase, self).__init__()
        self.generator = generator
        self.padding_idx = pad_id



    def _make_shard_state(self, batch, output,  attns=None):
        """
        Make shard state dictionary for shards() to return iterable
        shards for efficient loss computation. Subclass must define
        this method to match its own _compute_loss() interface.
        Args:
            batch: the current batch.
            output: the predict output from the model.
            range_: the range of examples for computing, the whole
                    batch or a trunc of it?
            attns: the attns dictionary returned from the model.
        """
        return NotImplementedError

    def _compute_loss(self, batch, output, target, **kwargs):
        """
        Compute the loss. Subclass must define this method.

        Args:

            batch: the current batch.
            output: the predict output from the model.
            target: the validate target to compare output with.
            **kwargs(optional): additional info for computing loss.
        """
        return NotImplementedError

    def monolithic_compute_loss(self, batch, output, mask_src,node_num=None,mask_graph=None,cos_sim=None, doc_word_cos_sim=None):
        """
        Compute the forward loss for the batch.

        Args:
          batch (batch): batch of labeled examples
          output (:obj:`FloatTensor`):
              output of decoder model `[tgt_len x batch x hidden]`
          attns (dict of :obj:`FloatTensor`) :
              dictionary of attention distributions
              `[tgt_len x batch x src_len]`
        Returns:
            :obj:`onmt.utils.Statistics`: loss statistics
        """
        shard_state = self._make_shard_state(batch, output,mask_src, node_num, mask_graph,cos_sim, doc_word_cos_sim)
        _, batch_stats = self._compute_loss(batch, **shard_state)

        return batch_stats

    def sharded_compute_loss(self, batch, output,
                              shard_size,
                             normalization, mask_src, node_num, graph, cos_sim, doc_word_cos_sim):
        """Compute the forward loss and backpropagate.  Computation is done
        with shards and optionally truncation for memory efficiency.

        Also supports truncated BPTT for long sequences by taking a
        range in the decoder output sequence to back propagate in.
        Range is from `(cur_trunc, cur_trunc + trunc_size)`.

        Note sharding is an exact efficiency trick to relieve memory
        required for the generation buffers. Truncation is an
        approximate efficiency trick to relieve the memory required
        in the RNN buffers.

        Args:
          batch (batch) : batch of labeled examples
          output (:obj:`FloatTensor`) :
              output of decoder model `[tgt_len x batch x hidden]`
          attns (dict) : dictionary of attention distributions
              `[tgt_len x batch x src_len]`
          cur_trunc (int) : starting position of truncation window
          trunc_size (int) : length of truncation window
          shard_size (int) : maximum number of examples in a shard
          normalization (int) : Loss is divided by this number

        Returns:
            :obj:`onmt.utils.Statistics`: validation loss statistics

        """
        batch_stats = Statistics()
        shard_state = self._make_shard_state(batch, output, mask_src, node_num, graph, cos_sim, doc_word_cos_sim)
        for shard in shards(shard_state, shard_size):
            loss, stats = self._compute_loss(batch, **shard)
            # ((loss+doc_word_contra_loss+contra_loss).div(float(normalization))).backward()
                # ((loss+contra_loss+doc_word_contra_loss).div(float(normalization))).backward()
            ((loss).div(float(normalization))).backward()
            batch_stats.update(stats)
            del loss
            del stats
        del graph
        del shard_state
        torch.cuda.empty_cache()

        return batch_stats

    def _stats(self, loss, scores, target, contra_loss, doc_word_contra_loss):
        """
        Args:
            loss (:obj:`FloatTensor`): the loss computed by the loss criterion.
            scores (:obj:`FloatTensor`): a score for each possible output
            target (:obj:`FloatTensor`): true targets

        Returns:
            :obj:`onmt.utils.Statistics` : statistics for this batch.
        """
        pred = scores.max(1)[1]
        non_padding = target.ne(self.padding_idx)
        num_correct = pred.eq(target) \
                          .masked_select(non_padding) \
                          .sum() \
                          .item()
        num_non_padding = non_padding.sum().item()
        contra_loss = contra_loss.item() if contra_loss != 0.0 else 0.0
        doc_word_contra_loss = doc_word_contra_loss.item() if doc_word_contra_loss != 0.0 else 0.0
        stat = Statistics(loss.item(), num_non_padding, num_correct, contra_loss, doc_word_contra_loss)
        return stat

    def _bottle(self, _v):
        return _v.view(-1, _v.size(2))

    def _unbottle(self, _v, batch_size):
        return _v.view(-1, batch_size, _v.size(1))


class LabelSmoothingLoss(nn.Module):
    """
    With label smoothing,
    KL-divergence between q_{smoothed ground truth prob.}(w)
    and p_{prob. computed by model}(w) is minimized.
    """
    def __init__(self, label_smoothing, tgt_vocab_size, ignore_index=-100):
        assert 0.0 < label_smoothing <= 1.0
        self.padding_idx = ignore_index
        super(LabelSmoothingLoss, self).__init__()

        smoothing_value = label_smoothing / (tgt_vocab_size - 2)
        one_hot = torch.full((tgt_vocab_size,), smoothing_value)
        one_hot[self.padding_idx] = 0
        self.register_buffer('one_hot', one_hot.unsqueeze(0))
        self.confidence = 1.0 - label_smoothing

    def forward(self, output, target):
        """
        output (FloatTensor): batch_size x n_classes
        target (LongTensor): batch_size
        """
        model_prob = self.one_hot.repeat(target.size(0), 1)
        model_prob.scatter_(1, target.unsqueeze(1), self.confidence)
        model_prob.masked_fill_((target == self.padding_idx).unsqueeze(1), 0)

        return F.kl_div(output, model_prob, reduction='sum')


class NMTLossCompute(LossComputeBase):
    """
    Standard NMT Loss Computation.
    """

    def __init__(self, generator, symbols, vocab_size,
                 label_smoothing=0.0):
        super(NMTLossCompute, self).__init__(generator, symbols['PAD'])
        self.sparse = not isinstance(generator[1], nn.LogSoftmax)
        if label_smoothing > 0:
            self.criterion = LabelSmoothingLoss(
                label_smoothing, vocab_size, ignore_index=self.padding_idx
            )
        else:
            self.criterion = nn.NLLLoss(
                ignore_index=self.padding_idx, reduction='sum'
            )
        self.loss = nn.CrossEntropyLoss(reduction='none')

    def _make_shard_state(self, batch, output, mask_src, node_num, graph, cos_sim, doc_word_cos_sim):
        return {
            "output": output,
            "cos_sim": cos_sim,
            "mask_src":mask_src,
            "node_num":node_num,
            "doc_word_cos_sim": doc_word_cos_sim,
            "graph": graph,
            "target": batch.tgt[:,1:],
        }

    def _ncontrast(self, x_dis, adj_label,mask_graph, tau=1):
        """
        compute the Ncontrast loss
        """
        x_dis = torch.exp(tau * x_dis)
        x_dis_sum = torch.sum(x_dis*mask_graph, 1)
        #print(x_dis.shape, mask_graph.shape, adj_label.shape)
        x_dis_sum_pos = torch.sum(x_dis * adj_label, 1)
        reverse_x_dis_sum = x_dis_sum.masked_fill(x_dis_sum == 0, 1) ** (-1)
        cum_prod = x_dis_sum_pos * reverse_x_dis_sum
        cum_prod = cum_prod.masked_fill(x_dis_sum == 0, 1)
        del x_dis_sum
        del x_dis_sum_pos
        del reverse_x_dis_sum
        loss = -torch.log(cum_prod)
        loss = loss.mean()
        return loss

    def _compute_loss(self, batch, output, target, mask_src, node_num, graph, cos_sim=None, doc_word_cos_sim=None):
        bottled_output = self._bottle(output)
        scores = self.generator(bottled_output)
        gtruth =target.contiguous().view(-1)

        loss = self.criterion(scores, gtruth)

        if cos_sim is not None:
            doc_word_cos_sim = doc_word_cos_sim.reshape(-1, doc_word_cos_sim.size(-1))
            labels = torch.ones(doc_word_cos_sim.size(0)).long().to(doc_word_cos_sim.device)
            doc_word_contra_loss = self.loss(doc_word_cos_sim.squeeze(0), labels)
            #print(doc_word_contra_loss.shape)
            mask_src = mask_src.reshape(mask_src.shape[0]*mask_src.shape[1],-1).squeeze(-1)
            doc_word_contra_loss = (doc_word_contra_loss * mask_src.float()).mean()

            nn = cos_sim.size(-2)
            #print(cos_sim.shape,nn,batch_size, negative_num)
            #print("contra_loss")
            #print(graph)
            #contra_loss = 0.0
            #for i in len(graph):
            #    each_graph = graph[i]
                #each_graph = np.concatenate((graph[i], np.zeros(2,node_num[i])), axis=0) 
                #each_graph = np.concatenate((each_graph, np.zeros((each_graph.shape[0],2))), axis=1)
            #each_contra_loss = self._ncontrast(cos_sim[i,:node_num[i]+2,:node_num[i]+2], each_graph)
            #contra_loss += each_contra_loss
            #contra_loss = contra_loss/len(graph)
            #print("contra_loss:", contra_loss)
            mask_vec = seq_len_to_mask(node_num, max_len=nn).to(cos_sim.device)
            mask_graph_row = torch.tile(mask_vec.unsqueeze(1), (1,nn,1))
            mask_graph_col = torch.tile(mask_vec.unsqueeze(2), (1, 1,nn))
            mask_graph = mask_graph_row*mask_graph_col
            #print(cos_sim)
            #print(cos_sim.shape, mask_graph.shape)
            #print(graph.shape)
            contra_loss = self._ncontrast(cos_sim, graph, mask_graph)
            #print(contra_loss, doc_word_contra_loss, loss)
        else:
            doc_word_contra_loss = 0.0
            contra_loss = 0.0
        contra_loss = contra_loss.clone() if contra_loss != 0.0 else 0.0
        doc_word_contra_loss = doc_word_contra_loss.clone() if doc_word_contra_loss != 0.0 else 0.0

        stats = self._stats(loss.clone(), scores, gtruth, contra_loss, doc_word_contra_loss)
        loss = loss + doc_word_contra_loss + contra_loss

        del contra_loss
        del doc_word_contra_loss

        return loss, stats


def filter_shard_state(state, shard_size=None):
    """ ? """
    for k, v in state.items():
        if shard_size is None:
            yield k, v

        if v is not None:
            v_split = []
            if isinstance(v, torch.Tensor):
                for v_chunk in torch.split(v, shard_size):
                    v_chunk = v_chunk.data.clone()
                    v_chunk.requires_grad = v.requires_grad
                    v_split.append(v_chunk)
            yield k, (v, v_split)


def shards(state, shard_size, eval_only=False):
    """
    Args:
        state: A dictionary which corresponds to the output of
               *LossCompute._make_shard_state(). The values for
               those keys are Tensor-like or None.
        shard_size: The maximum size of the shards yielded by the model.
        eval_only: If True, only yield the state, nothing else.
              Otherwise, yield shards.

    Yields:
        Each yielded shard is a dict.

    Side effect:
        After the last shard, this function does back-propagation.
    """
    if eval_only:
        yield filter_shard_state(state)
    else:
        # non_none: the subdict of the state dictionary where the values
        # are not None.
        non_none = dict(filter_shard_state(state, shard_size))

        # Now, the iteration:
        # state is a dictionary of sequences of tensor-like but we
        # want a sequence of dictionaries of tensors.
        # First, unzip the dictionary into a sequence of keys and a
        # sequence of tensor-like sequences.
        keys, values = zip(*((k, [v_chunk for v_chunk in v_split])
                             for k, (_, v_split) in non_none.items()))

        # Now, yield a dictionary for each shard. The keys are always
        # the same. values is a sequence of length #keys where each
        # element is a sequence of length #shards. We want to iterate
        # over the shards, not over the keys: therefore, the values need
        # to be re-zipped by shard and then each shard can be paired
        # with the keys.
        for shard_tensors in zip(*values):
            yield dict(zip(keys, shard_tensors))

        # Assumed backprop'd
        # if True:
            # return
        variables = []
        for k, (v, v_split) in non_none.items():
            if isinstance(v, torch.Tensor) and state[k].requires_grad:
                variables.extend(zip(torch.split(state[k], shard_size),
                                     [v_chunk.grad for v_chunk in v_split]))
        inputs, grads = zip(*variables)
        if None not in grads:
            torch.autograd.backward(inputs, grads, retain_graph=True)
