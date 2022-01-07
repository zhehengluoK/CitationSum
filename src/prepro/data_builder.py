import gc
import glob
from  tqdm import tqdm
import hashlib
import itertools
import json
import os
import random
import re
import pandas as pd
import time
import dgl
import subprocess
from collections import Counter, deque
from os.path import join as pjoin
from nltk.tokenize import sent_tokenize, word_tokenize

import torch
from multiprocess import Pool

from others.logging import logger
from others.tokenization import BertTokenizer
from pytorch_transformers import XLNetTokenizer

from others.utils import clean
from prepro.utils import _get_word_ngrams

import xml.etree.ElementTree as ET

nyt_remove_words = ["photo", "graph", "chart", "map", "table", "drawing"]


tokenizer = BertTokenizer.from_pretrained('bert-base-uncased', do_lower_case=True)
def recover_from_corenlp(s):
    s = re.sub(r' \'{\w}', '\'\g<1>', s)
    s = re.sub(r'\'\' {\w}', '\'\'\g<1>', s)



def load_json(p, lower):
    source = []
    tgt = []
    flag = False
    for sent in json.load(open(p))['sentences']:
        tokens = [t['word'] for t in sent['tokens']]
        if (lower):
            tokens = [t.lower() for t in tokens]
        if (tokens[0] == '@highlight'):
            flag = True
            tgt.append([])
            continue
        if (flag):
            tgt[-1].extend(tokens)
        else:
            source.append(tokens)

    source = [clean(' '.join(sent)).split() for sent in source]
    tgt = [clean(' '.join(sent)).split() for sent in tgt]
    return source, tgt



def load_xml(p):
    tree = ET.parse(p)
    root = tree.getroot()
    title, byline, abs, paras = [], [], [], []
    title_node = list(root.iter('hedline'))
    if (len(title_node) > 0):
        try:
            title = [p.text.lower().split() for p in list(title_node[0].iter('hl1'))][0]
        except:
            print(p)

    else:
        return None, None
    byline_node = list(root.iter('byline'))
    byline_node = [n for n in byline_node if n.attrib['class'] == 'normalized_byline']
    if (len(byline_node) > 0):
        byline = byline_node[0].text.lower().split()
    abs_node = list(root.iter('abstract'))
    if (len(abs_node) > 0):
        try:
            abs = [p.text.lower().split() for p in list(abs_node[0].iter('p'))][0]
        except:
            print(p)

    else:
        return None, None
    abs = ' '.join(abs).split(';')
    abs[-1] = abs[-1].replace('(m)', '')
    abs[-1] = abs[-1].replace('(s)', '')

    for ww in nyt_remove_words:
        abs[-1] = abs[-1].replace('(' + ww + ')', '')
    abs = [p.split() for p in abs]
    abs = [p for p in abs if len(p) > 2]

    for doc_node in root.iter('block'):
        att = doc_node.get('class')
        # if(att == 'abstract'):
        #     abs = [p.text for p in list(f.iter('p'))]
        if (att == 'full_text'):
            paras = [p.text.lower().split() for p in list(doc_node.iter('p'))]
            break
    if (len(paras) > 0):
        if (len(byline) > 0):
            paras = [title + ['[unused3]'] + byline + ['[unused4]']] + paras
        else:
            paras = [title + ['[unused3]']] + paras

        return paras, abs
    else:
        return None, None


def tokenize(args):
    stories_dir = os.path.abspath(args.raw_path)
    tokenized_stories_dir = os.path.abspath(args.save_path)

    print("Preparing to tokenize %s to %s..." % (stories_dir, tokenized_stories_dir))
    stories = os.listdir(stories_dir)
    # make IO list file
    print("Making list of files to tokenize...")
    with open("mapping_for_corenlp.txt", "w") as f:
        for s in stories:
            if (not s.endswith('story')):
                continue
            f.write("%s\n" % (os.path.join(stories_dir, s)))
    command = ['java', 'edu.stanford.nlp.pipeline.StanfordCoreNLP', '-annotators', 'tokenize,ssplit',
               '-ssplit.newlineIsSentenceBreak', 'always', '-filelist', 'mapping_for_corenlp.txt', '-outputFormat',
               'json', '-outputDirectory', tokenized_stories_dir]
    print("Tokenizing %i files in %s and saving in %s..." % (len(stories), stories_dir, tokenized_stories_dir))
    subprocess.call(command)
    print("Stanford CoreNLP Tokenizer has finished.")
    os.remove("mapping_for_corenlp.txt")

    # Check that the tokenized stories directory contains the same number of files as the original directory
    num_orig = len(os.listdir(stories_dir))
    num_tokenized = len(os.listdir(tokenized_stories_dir))
    if num_orig != num_tokenized:
        raise Exception(
            "The tokenized stories directory %s contains %i files, but it should contain the same number as %s (which has %i files). Was there an error during tokenization?" % (
                tokenized_stories_dir, num_tokenized, stories_dir, num_orig))
    print("Successfully finished tokenizing %s to %s.\n" % (stories_dir, tokenized_stories_dir))


def generate_graph_structs(args, paper_id, graph_strut_dict):
    sub_graph_dict = {}
    sub_graph_set = []

    n_hop = args.n_hop
    max_neighbor_num = args.max_neighbor_num
    k_nbrs = _k_hop_neighbor(paper_id, n_hop, max_neighbor_num, graph_strut_dict)
    for sub_g in k_nbrs:
        sub_graph_set += sub_g

    for node in sub_graph_set:
        sub_graph_dict[node] = []

    for sub_g in k_nbrs:
        for centre_node in sub_g:
            nbrs = graph_strut_dict[centre_node]['references']
            c_nbrs = list(set(nbrs).intersection(sub_graph_set))
            sub_graph_dict[centre_node].extend(c_nbrs)
            for c_nbr in c_nbrs:
                sub_graph_dict[c_nbr].append(centre_node)
    # in python 3.6, the first in subgraph dict is source paper
    return sub_graph_dict

def _k_hop_neighbor(paper_id, n_hop, max_neighbor, graph_strut_dict):
    sub_graph = [[] for _ in range(n_hop + 1)]
    level = 0
    visited = set()
    q = deque()
    q.append([paper_id, level])
    curr_node_num = 0
    while len(q) != 0:
        paper_first = q.popleft()
        paper_id_first, level_first = paper_first
        if level_first > n_hop:
            return sub_graph
        sub_graph[level_first].append(paper_id_first)
        curr_node_num += 1
        if curr_node_num > max_neighbor:
            return sub_graph
        visited.add(paper_id_first)
        for pid in graph_strut_dict[paper_id_first]["references"]:
            if pid not in visited and pid in graph_strut_dict:
                q.append([pid, level_first + 1])
                visited.add(pid)

    return sub_graph


def generate_dgl_graph(paper_id, graph_struct, nodes_num):
    g = dgl.DGLGraph()
    assert len(graph_struct) == nodes_num

    g.add_nodes(len(graph_struct))
    pid2idx = {}
    for index, key_node in enumerate(graph_struct):
        pid2idx[key_node] = index
    assert pid2idx[paper_id] == 0

    for index, key_node in enumerate(graph_struct):
        neighbor = [pid2idx[node] for node in graph_struct[key_node]]
        # add self loop
        neighbor.append(index)
        key_nodes = [index] * len(neighbor)
        g.add_edges(key_nodes, neighbor)
    return g

def generate_graph_inputs(args, graph_struct, graph_strut_dict):
    graph_inputs = []
    for pid in graph_struct:
        graph_i = graph_strut_dict[pid][args.graph_input_type]
        graph_input = clean([j for sub in graph_i for j in sub])
        graph_inputs.append(graph_input)
    graph_inputs = []
    for input in graph_inputs[1:]:
        tokenize_graph_input = [word_tokenize(t) for t in sent_tokenize(input)]
        tokenize_graph_input = tokenize_graph_input[:args.max_src_nsents]
        sent_label = greedy_selection(tokenize_graph_input, abstract, 3)
        sent_label_id = range(tokenize_graph_input)
        graph_input = []
        for i in sent_label:
            sent_label_id.remove(i)
            graph_input.append(tokenize_graph_input[i])
        for iter in range(args.negative_number):
            #if 3 < len(sent_label_id):
            idx = random.sample(sent_label_id, 3)
            each_input = []
            for j in idx:
                each_input.append(tokenize_graph_input[j])
            graph_input.append(each_input)
        graph_inputs.append(graph_input)

    return graph_inputs

def format_cite(args):
    root_data_dir = os.path.abspath(args.raw_path)
    dirs = ['train', 'val', 'test']

    #print("Preparing to tokenize %s to %s..." % (stories_dir, tokenized_stories_dir))

    if args.setting == "transductive":
        graph_strut_dict = {}
        for dir in dirs:
            source_txt_file = os.path.join(root_data_dir, '{}.jsonl'.format(dir))
            #df = pd.read_json(path_or_buf=source_txt_file, lines=True)
            
            with open(source_txt_file, 'r') as f:
                json_main = list(f)
            
            for ins in json_main:
                ins = json.loads(ins)
                graph_strut_dict[ins["paper_id"]] = ins
    else:
        graph_train_dict = {}
        graph_val_dict = {}
        graph_test_dict = {}

        test_path = os.path.join(root_data_dir, 'test.jsonl')
        val_path = os.path.join(root_data_dir, 'val.jsonl')
        train_path = os.path.join(root_data_dir, 'train.jsonl')

        #train_df = pd.read_json(path_or_buf=train_path, lines=True)
        #val_df = pd.read_json(path_or_buf=val_path, lines=True)
        #test_df = pd.read_json(path_or_buf=test_path, lines=True)

        with open(train_path, 'r') as f:
            train_json_main = list(f)

        with open(val_path, 'r') as f:
            val_json_main = list(f)
        
        with open(test_path, 'r') as f:
            test_json_main = list(f)
        
        for ins in train_json_main:
            ins = json.loads(ins)
            graph_train_dict[ins["paper_id"]] = ins

        for ins in val_json_main:
            ins = json.loads(ins)
            graph_val_dict[ins["paper_id"]] = ins

        for ins in test_json_main:
            ins = json.loads(ins)
            graph_test_dict[ins["paper_id"]] = ins

        graph = {'train': graph_train_dict, 'val': graph_val_dict, 'test': graph_test_dict}

    data_dict = {}


    for corpus in dirs:
        data_lst = []
        source_txt_file = os.path.join(root_data_dir, '{}.jsonl'.format(corpus))
        #df = pd.read_json(path_or_buf=source_txt_file, lines=True)

        with open(source_txt_file, 'r') as f:
            json_main = list(f)
        
        for row in tqdm(json_main):
            row = json.loads(row)
            pid = row['paper_id']
            introduction = [j for sub in row['introduction'] for j in sub]
            abstract = [word_tokenize(t) for t in sent_tokenize(clean(row['abstract']))]
            introduce = [word_tokenize(t) for t in sent_tokenize(clean(introduction))]
            if args.setting == "transductive":
                sub_graph_dict = generate_graph_structs(args, pid, graph_strut_dict)
                graph_text = generate_graph_inputs(args,ub_graph_dict, graph_strut_dict, abstract)
            else:
                sub_graph_dict = generate_graph_structs(args, pid, graph[corpus])
                graph_text = generate_graph_inputs(args, sub_graph_dict, graph[corpus], abstract)
            node_num = len(graph_text)+1
            data_lst.append((corpus, pid, abstract, introduce, sub_graph_dict, graph_text, node_num, args))
        data_dict[corpus] = data_lst

    for d in dirs:
        a_lst = data_dict[d]
        pool = Pool(args.n_cpus)
        dataset = []
        shard_count = 0
        with tqdm(total=len(a_lst)) as pbar:
            with tqdm(total=args.shard_size) as spbar:
                for i, data in enumerate(pool.imap(_format_cite, a_lst)):
                    if data:
                        src_subtoken_idxs, sent_labels, tgt_subtoken_idxs, segments_ids, cls_ids, src_txt, tgt_txt, graph_subtoken_idxs, graph = data
                        
                        #print("graph_idxs:", graph_subtoken_idxs)
                        #print("graph:", graph)
                        #print("len:", len(graph_subtoken_idxs))
                        b_data_dict = {"src": src_subtoken_idxs, "tgt": tgt_subtoken_idxs,
                                       "src_sent_labels": sent_labels, "segs": segments_ids, 'clss': cls_ids,
                                       'src_txt': src_txt, "tgt_txt": tgt_txt, 'graph_src':graph_subtoken_idxs, "graph": graph}
                        dataset.append(b_data_dict)
                    spbar.update()
                    if (len(dataset) > args.shard_size):
                        fpath = "{:s}/{:s}.{:d}.pt".format(args.save_path, d, shard_count)
                        torch.save(dataset,fpath)
                        dataset = []
                        shard_count += 1
                        pbar.update()
                        spbar.reset()
                    # gc.collect()
                spbar.close()
                pbar.close()
            pool.close()
            pool.join()
            if len(dataset) > 0:
                fpath = "{:s}/{:s}.{:d}.pt".format(args.save_path, d, shard_count)
                torch.save(dataset,fpath)
                shard_count += 1
        #end = time.time()
        #print('... Ending (4), time elapsed {}'.format(end - start))

def _format_cite(params):
    corpus_type, pid, abstract, introduce, sub_graph_dict, graph_text, node_num, args = params
    is_test = corpus_type == 'test'
    bert = BertCiteData(args)
    sent_labels = greedy_selection(introduce[:args.max_src_nsents], abstract, 3)
    graph = generate_dgl_graph(pid, sub_graph_dict, node_num)
    b_data = bert.preprocess(introduce, abstract, sent_labels, graph_text, graph, use_bert_basic_tokenizer=args.use_bert_basic_tokenizer,
                             is_test=is_test)
    return b_data

def cal_rouge(evaluated_ngrams, reference_ngrams):
    reference_count = len(reference_ngrams)
    evaluated_count = len(evaluated_ngrams)

    overlapping_ngrams = evaluated_ngrams.intersection(reference_ngrams)
    overlapping_count = len(overlapping_ngrams)

    if evaluated_count == 0:
        precision = 0.0
    else:
        precision = overlapping_count / evaluated_count

    if reference_count == 0:
        recall = 0.0
    else:
        recall = overlapping_count / reference_count

    f1_score = 2.0 * ((precision * recall) / (precision + recall + 1e-8))
    return {"f": f1_score, "p": precision, "r": recall}


def greedy_selection(doc_sent_list, abstract_sent_list, summary_size):
    def _rouge_clean(s):
        return re.sub(r'[^a-zA-Z0-9 ]', '', s)

    max_rouge = 0.0
    abstract = sum(abstract_sent_list, [])
    abstract = _rouge_clean(' '.join(abstract)).split()
    sents = [_rouge_clean(' '.join(s)).split() for s in doc_sent_list]
    evaluated_1grams = [_get_word_ngrams(1, [sent]) for sent in sents]
    reference_1grams = _get_word_ngrams(1, [abstract])
    evaluated_2grams = [_get_word_ngrams(2, [sent]) for sent in sents]
    reference_2grams = _get_word_ngrams(2, [abstract])

    selected = []
    for s in range(summary_size):
        cur_max_rouge = max_rouge
        cur_id = -1
        for i in range(len(sents)):
            if (i in selected):
                continue
            c = selected + [i]
            candidates_1 = [evaluated_1grams[idx] for idx in c]
            candidates_1 = set.union(*map(set, candidates_1))
            candidates_2 = [evaluated_2grams[idx] for idx in c]
            candidates_2 = set.union(*map(set, candidates_2))
            rouge_1 = cal_rouge(candidates_1, reference_1grams)['f']
            rouge_2 = cal_rouge(candidates_2, reference_2grams)['f']
            rouge_score = rouge_1 + rouge_2
            if rouge_score > cur_max_rouge:
                cur_max_rouge = rouge_score
                cur_id = i
        if (cur_id == -1):
            return selected
        selected.append(cur_id)
        max_rouge = cur_max_rouge

    return sorted(selected)


def hashhex(s):
    """Returns a heximal formated SHA1 hash of the input string."""
    h = hashlib.sha1()
    h.update(s.encode('utf-8'))
    return h.hexdigest()


class BertData():
    def __init__(self, args):
        self.args = args
        self.tokenizer = BertTokenizer.from_pretrained('bert-base-uncased', do_lower_case=True)

        self.sep_token = '[SEP]'
        self.cls_token = '[CLS]'
        self.pad_token = '[PAD]'
        self.tgt_bos = '[unused0]'
        self.tgt_eos = '[unused1]'
        self.tgt_sent_split = '[unused2]'
        self.sep_vid = self.tokenizer.vocab[self.sep_token]
        self.cls_vid = self.tokenizer.vocab[self.cls_token]
        self.pad_vid = self.tokenizer.vocab[self.pad_token]

    def preprocess(self, src, tgt, sent_labels, use_bert_basic_tokenizer=False, is_test=False):

        if ((not is_test) and len(src) == 0):
            return None

        original_src_txt = [' '.join(s) for s in src]

        idxs = [i for i, s in enumerate(src) if (len(s) > self.args.min_src_ntokens_per_sent)]

        _sent_labels = [0] * len(src)
        for l in sent_labels:
            _sent_labels[l] = 1

        src = [src[i][:self.args.max_src_ntokens_per_sent] for i in idxs]
        sent_labels = [_sent_labels[i] for i in idxs]
        src = src[:self.args.max_src_nsents]
        sent_labels = sent_labels[:self.args.max_src_nsents]

        if ((not is_test) and len(src) < self.args.min_src_nsents):
            return None

        src_txt = [' '.join(sent) for sent in src]
        text = ' {} {} '.format(self.sep_token, self.cls_token).join(src_txt)

        src_subtokens = self.tokenizer.tokenize(text)

        src_subtokens = [self.cls_token] + src_subtokens + [self.sep_token]
        src_subtoken_idxs = self.tokenizer.convert_tokens_to_ids(src_subtokens)
        _segs = [-1] + [i for i, t in enumerate(src_subtoken_idxs) if t == self.sep_vid]
        segs = [_segs[i] - _segs[i - 1] for i in range(1, len(_segs))]
        segments_ids = []
        for i, s in enumerate(segs):
            if (i % 2 == 0):
                segments_ids += s * [0]
            else:
                segments_ids += s * [1]
        cls_ids = [i for i, t in enumerate(src_subtoken_idxs) if t == self.cls_vid]
        sent_labels = sent_labels[:len(cls_ids)]

        tgt_subtokens_str = '[unused0] ' + ' [unused2] '.join(
            [' '.join(self.tokenizer.tokenize(' '.join(tt), use_bert_basic_tokenizer=use_bert_basic_tokenizer)) for tt in tgt]) + ' [unused1]'
        tgt_subtoken = tgt_subtokens_str.split()[:self.args.max_tgt_ntokens]
        if ((not is_test) and len(tgt_subtoken) < self.args.min_tgt_ntokens):
            return None

        tgt_subtoken_idxs = self.tokenizer.convert_tokens_to_ids(tgt_subtoken)

        tgt_txt = '<q>'.join([' '.join(tt) for tt in tgt])
        src_txt = [original_src_txt[i] for i in idxs]

        return src_subtoken_idxs, sent_labels, tgt_subtoken_idxs, segments_ids, cls_ids, src_txt, tgt_txt

class BertCiteData():
    def __init__(self, args):
        self.args = args
        #self.tokenizer = BertTokenizer.from_pretrained('bert-base-uncased', do_lower_case=True)

        self.sep_token = '[SEP]'
        self.cls_token = '[CLS]'
        self.pad_token = '[PAD]'
        self.tgt_bos = '[unused0]'
        self.tgt_eos = '[unused1]'
        self.tgt_sent_split = '[unused2]'
        self.sep_vid = tokenizer.vocab[self.sep_token]
        self.cls_vid = tokenizer.vocab[self.cls_token]
        self.pad_vid = tokenizer.vocab[self.pad_token]

    def preprocess(self, src, tgt, sent_labels, graph_src, graph, use_bert_basic_tokenizer=False, is_test=False):

        if ((not is_test) and len(src) == 0):
            return None

        original_src_txt = [' '.join(s) for s in src]

        idxs = [i for i, s in enumerate(src) if (len(s) > self.args.min_src_ntokens_per_sent)]

        _sent_labels = [0] * len(src)
        for l in sent_labels:
            _sent_labels[l] = 1

        src = [src[i][:self.args.max_src_ntokens_per_sent] for i in idxs]
        sent_labels = [_sent_labels[i] for i in idxs]
        src = src[:self.args.max_src_nsents]
        sent_labels = sent_labels[:self.args.max_src_nsents]
        #graph_src = graph_src[:self.args.max_neighbor_num]

        if ((not is_test) and len(src) < self.args.min_src_nsents):
            return None

        src_txt = [' '.join(sent) for sent in src]
        text = ' {} {} '.format(self.sep_token, self.cls_token).join(src_txt)
        graph_subtoken_idxs = []
        for each_graph_srcs in graph_src:
            subtoken_idxs = []
            for each_graph_src in each_graph_srcs:
                each_src = [' '.join(sent) for sent in each_graph_src]
                each_text = ' {} {} '.format(self.sep_token, self.cls_token).join(each_src)
                each_graph_src_subtokens = tokenizer.tokenize(each_text)
                each_graph_src_subtokens = [self.cls_token] + each_graph_src_subtokens + [self.sep_token]
                each_graph_subtoken_idxs = tokenizer.convert_tokens_to_ids(each_graph_src_subtokens)
                subtoken_idxs.append(each_graph_subtoken_idxs)
            graph_subtoken_idxs.append(subtoken_idxs)

        src_subtokens = tokenizer.tokenize(text)
        src_subtokens = [self.cls_token] + src_subtokens + [self.sep_token]
        src_subtoken_idxs = tokenizer.convert_tokens_to_ids(src_subtokens)
        _segs = [-1] + [i for i, t in enumerate(src_subtoken_idxs) if t == self.sep_vid]
        segs = [_segs[i] - _segs[i - 1] for i in range(1, len(_segs))]
        segments_ids = []
        for i, s in enumerate(segs):
            if (i % 2 == 0):
                segments_ids += s * [0]
            else:
                segments_ids += s * [1]
        cls_ids = [i for i, t in enumerate(src_subtoken_idxs) if t == self.cls_vid]
        sent_labels = sent_labels[:len(cls_ids)]

        tgt_subtokens_str = '[unused0] ' + ' [unused2] '.join(
            [' '.join(tokenizer.tokenize(' '.join(tt), use_bert_basic_tokenizer=use_bert_basic_tokenizer)) for tt in tgt]) + ' [unused1]'
        tgt_subtoken = tgt_subtokens_str.split()[:self.args.max_tgt_ntokens]
        if ((not is_test) and len(tgt_subtoken) < self.args.min_tgt_ntokens):
            return None

        tgt_subtoken_idxs = tokenizer.convert_tokens_to_ids(tgt_subtoken)

        tgt_txt = '<q>'.join([' '.join(tt) for tt in tgt])
        src_txt = [original_src_txt[i] for i in idxs]

        return src_subtoken_idxs, sent_labels, tgt_subtoken_idxs, segments_ids, cls_ids, src_txt, tgt_txt, graph_subtoken_idxs, graph

def format_to_bert(args):
    if (args.dataset != ''):
        datasets = [args.dataset]
    else:
        datasets = ['train', 'valid', 'test']
    for corpus_type in datasets:
        a_lst = []
        for json_f in glob.glob(pjoin(args.raw_path, '*' + corpus_type + '.*.json')):
            real_name = json_f.split('/')[-1]
            a_lst.append((corpus_type, json_f, args, pjoin(args.save_path, real_name.replace('json', 'bert.pt'))))
        print(a_lst)
        pool = Pool(args.n_cpus)
        for d in pool.imap(_format_to_bert, a_lst):
            pass

        pool.close()
        pool.join()


def _format_to_bert(params):
    corpus_type, json_file, args, save_file = params
    is_test = corpus_type == 'test'
    if (os.path.exists(save_file)):
        logger.info('Ignore %s' % save_file)
        return

    bert = BertData(args)

    logger.info('Processing %s' % json_file)
    jobs = json.load(open(json_file))
    datasets = []
    for d in jobs:
        source, tgt = d['src'], d['tgt']

        sent_labels = greedy_selection(source[:args.max_src_nsents], tgt, 3)
        if (args.lower):
            source = [' '.join(s).lower().split() for s in source]
            tgt = [' '.join(s).lower().split() for s in tgt]
        b_data = bert.preprocess(source, tgt, sent_labels, use_bert_basic_tokenizer=args.use_bert_basic_tokenizer,
                                 is_test=is_test)
        # b_data = bert.preprocess(source, tgt, sent_labels, use_bert_basic_tokenizer=args.use_bert_basic_tokenizer)

        if (b_data is None):
            continue
        src_subtoken_idxs, sent_labels, tgt_subtoken_idxs, segments_ids, cls_ids, src_txt, tgt_txt = b_data
        b_data_dict = {"src": src_subtoken_idxs, "tgt": tgt_subtoken_idxs,
                       "src_sent_labels": sent_labels, "segs": segments_ids, 'clss': cls_ids,
                       'src_txt': src_txt, "tgt_txt": tgt_txt}
        datasets.append(b_data_dict)
    logger.info('Processed instances %d' % len(datasets))
    logger.info('Saving to %s' % save_file)
    torch.save(datasets, save_file)
    datasets = []
    gc.collect()


def format_to_lines(args):
    corpus_mapping = {}
    for corpus_type in ['valid', 'test', 'train']:
        temp = []
        for line in open(pjoin(args.map_path, 'mapping_' + corpus_type + '.txt')):
            temp.append(hashhex(line.strip()))
        corpus_mapping[corpus_type] = {key.strip(): 1 for key in temp}
    train_files, valid_files, test_files = [], [], []
    for f in glob.glob(pjoin(args.raw_path, '*.json')):
        real_name = f.split('/')[-1].split('.')[0]
        if (real_name in corpus_mapping['valid']):
            valid_files.append(f)
        elif (real_name in corpus_mapping['test']):
            test_files.append(f)
        elif (real_name in corpus_mapping['train']):
            train_files.append(f)
        # else:
        #     train_files.append(f)

    corpora = {'train': train_files, 'valid': valid_files, 'test': test_files}
    for corpus_type in ['train', 'valid', 'test']:
        a_lst = [(f, args) for f in corpora[corpus_type]]
        pool = Pool(args.n_cpus)
        dataset = []
        p_ct = 0
        for d in pool.imap_unordered(_format_to_lines, a_lst):
            dataset.append(d)
            if (len(dataset) > args.shard_size):
                pt_file = "{:s}.{:s}.{:d}.json".format(args.save_path, corpus_type, p_ct)
                with open(pt_file, 'w') as save:
                    # save.write('\n'.join(dataset))
                    save.write(json.dumps(dataset))
                    p_ct += 1
                    dataset = []

        pool.close()
        pool.join()
        if (len(dataset) > 0):
            pt_file = "{:s}.{:s}.{:d}.json".format(args.save_path, corpus_type, p_ct)
            with open(pt_file, 'w') as save:
                # save.write('\n'.join(dataset))
                save.write(json.dumps(dataset))
                p_ct += 1
                dataset = []


def _format_to_lines(params):
    f, args = params
    print(f)
    source, tgt = load_json(f, args.lower)
    return {'src': source, 'tgt': tgt}




def format_xsum_to_lines(args):
    if (args.dataset != ''):
        datasets = [args.dataset]
    else:
        datasets = ['train', 'test', 'valid']

    corpus_mapping = json.load(open(pjoin(args.raw_path, 'XSum-TRAINING-DEV-TEST-SPLIT-90-5-5.json')))

    for corpus_type in datasets:
        mapped_fnames = corpus_mapping[corpus_type]
        root_src = pjoin(args.raw_path, 'restbody')
        root_tgt = pjoin(args.raw_path, 'firstsentence')
        # realnames = [fname.split('.')[0] for fname in os.listdir(root_src)]
        realnames = mapped_fnames

        a_lst = [(root_src, root_tgt, n) for n in realnames]
        pool = Pool(args.n_cpus)
        dataset = []
        p_ct = 0
        for d in pool.imap_unordered(_format_xsum_to_lines, a_lst):
            if (d is None):
                continue
            dataset.append(d)
            if (len(dataset) > args.shard_size):
                pt_file = "{:s}.{:s}.{:d}.json".format(args.save_path, corpus_type, p_ct)
                with open(pt_file, 'w') as save:
                    save.write(json.dumps(dataset))
                    p_ct += 1
                    dataset = []

        pool.close()
        pool.join()
        if (len(dataset) > 0):
            pt_file = "{:s}.{:s}.{:d}.json".format(args.save_path, corpus_type, p_ct)
            with open(pt_file, 'w') as save:
                save.write(json.dumps(dataset))
                p_ct += 1
                dataset = []


def _format_xsum_to_lines(params):
    src_path, root_tgt, name = params
    f_src = pjoin(src_path, name + '.restbody')
    f_tgt = pjoin(root_tgt, name + '.fs')
    if (os.path.exists(f_src) and os.path.exists(f_tgt)):
        print(name)
        source = []
        for sent in open(f_src):
            source.append(sent.split())
        tgt = []
        for sent in open(f_tgt):
            tgt.append(sent.split())
        return {'src': source, 'tgt': tgt}
    return None
