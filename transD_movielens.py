from comet_ml import Experiment
import torch
import torch.nn as nn
import torch.nn.functional as F
import shutil
import torch.optim as optim
from torch.autograd import Variable
from torch.utils.data import Dataset, DataLoader
from torch.nn.init import xavier_normal, xavier_uniform
from torch.distributions import Categorical
from sklearn.metrics import precision_recall_fscore_support
from sklearn.metrics import roc_auc_score, accuracy_score
import numpy as np
import random
import argparse
import pickle
import json
import logging
import sys, os
import subprocess
from tqdm import tqdm
tqdm.monitor_interval = 0
from utils import create_or_append, compute_rank
from preprocess_movie_lens import make_dataset
import joblib
from collections import Counter
import ipdb
sys.path.append('../')
import gc
from collections import OrderedDict
from model import *

ftensor = torch.FloatTensor
ltensor = torch.LongTensor
v2np = lambda v: v.data.cpu().numpy()
USE_SPARSE_EMB = True

def optimizer(params, mode, *args, **kwargs):
    if mode == 'SGD':
        opt = optim.SGD(params, *args, momentum=0., **kwargs)
    elif mode.startswith('nesterov'):
        momentum = float(mode[len('nesterov'):])
        opt = optim.SGD(params, *args, momentum=momentum, nesterov=True, **kwargs)
    elif mode.lower() == 'adam':
        betas = kwargs.pop('betas', (.9, .999))
        opt = optim.Adam(params, *args, betas=betas, amsgrad=True, **kwargs)
    elif mode.lower() == 'adam_hyp2':
        betas = kwargs.pop('betas', (.5, .99))
        opt = optim.Adam(params, *args, betas=betas, amsgrad=True, **kwargs)
    elif mode.lower() == 'adam_hyp3':
        betas = kwargs.pop('betas', (0., .99))
        opt = optim.Adam(params, *args, betas=betas, amsgrad=True, **kwargs)
    elif mode.lower() == 'adam_sparse':
        betas = kwargs.pop('betas', (.9, .999))
        opt = optim.SparseAdam(params, *args, betas=betas)
    elif mode.lower() == 'adam_sparse_hyp2':
        betas = kwargs.pop('betas', (.5, .99))
        opt = optim.SparseAdam(params, *args, betas=betas)
    elif mode.lower() == 'adam_sparse_hyp3':
        betas = kwargs.pop('betas', (.0, .99))
        opt = optim.SparseAdam(params, *args, betas=betas)
    else:
        raise NotImplementedError()
    return opt

def lr_scheduler(optimizer, decay_lr, num_epochs):
    if decay_lr in ('ms1', 'ms2', 'ms3'):
        decay_lr = int(decay_lr[-1])
        lr_milestones = [2 ** x for x in xrange(10-decay_lr, 10) if 2 ** x < num_epochs]
        scheduler = optim.lr_scheduler.MultiStepLR(optimizer, milestones=lr_milestones, gamma=0.1)

    elif decay_lr.startswith('step_exp_'):
        gamma = float(decay_lr[len('step_exp_'):])
        scheduler = optim.lr_scheduler.ExponentialLR(optimizer, gamma=gamma)

    elif decay_lr.startswith('halving_step'):
        step_size = int(decay_lr[len('halving_step'):])
        scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=step_size, gamma=0.5)

    elif decay_lr.startswith('ReduceLROnPlateau'):
        scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, cooldown=10, threshold=1e-3, factor=0.1, min_lr=1e-7, verbose=True)

    elif decay_lr == '':
        scheduler = None
    else:
        raise NotImplementedError()

    return scheduler

def freeze_model(model):
    model.eval()
    for params in model.parameters():
        params.requires_grad = False

def roc_auc_score_multiclass(actual_class, pred_class, average = "macro"):

    #creating a set of all the unique classes using the actual class list
    unique_class = set(actual_class)
    roc_auc_dict = {}
    for per_class in unique_class:
        #creating a list of all the classes except the current class
        other_class = [x for x in unique_class if x != per_class]

        #marking the current class as 1 and all other classes as 0
        new_actual_class = [0 if x in other_class else 1 for x in actual_class]
        new_pred_class = [0 if x in other_class else 1 for x in pred_class]

        #using the sklearn metrics method to calculate the roc_auc_score
        roc_auc = roc_auc_score(new_actual_class, new_pred_class, average = average)
        roc_auc_dict[per_class] = roc_auc

    return roc_auc_dict

class MarginRankingLoss(nn.Module):
    def __init__(self, margin):
        super(MarginRankingLoss, self).__init__()
        self.margin = margin

    def forward(self, p_enrgs, n_enrgs, weights=None):
        scores = (self.margin + p_enrgs - n_enrgs).clamp(min=0)

        if weights is not None:
            scores = scores * weights / weights.mean()

        return scores.mean(), scores

_cb_var_user = []
_cb_var_movie = []
def corrupt_batch(batch, num_ent, num_users, num_movies):
    # batch: ltensor type, contains positive triplets
    batch_size, _ = batch.size()

    corrupted = batch.clone()

    if len(_cb_var_user) == 0 and len(_cb_var_movie) == 0:
        _cb_var_user.append(ltensor(batch_size//2).cuda())
        _cb_var_movie.append(ltensor(batch_size//2).cuda())

    q_samples_l = _cb_var_user[0].random_(0, num_users)
    q_samples_r = _cb_var_movie[0].random_(num_users, num_users + num_movies - 1)

    corrupted[:batch_size//2, 0] = q_samples_l
    corrupted[batch_size//2:, 2] = q_samples_r

    return corrupted.contiguous(), torch.cat([q_samples_l, q_samples_r])

'''Monitor Norm of gradients'''
def monitor_grad_norm(model):
    parameters = list(filter(lambda p: p.grad is not None, model.parameters()))
    total_norm = 0
    for p in parameters:
        param_norm = p.grad.data.norm(2)
        total_norm += param_norm ** 2
    total_norm = total_norm ** (1. / 2)
    return total_norm

'''Monitor Norm of weights'''
def monitor_weight_norm(model):
    parameters = list(filter(lambda p: p is not None, model.parameters()))
    total_norm = 0
    for p in parameters:
        param_norm = p.data.norm(2)
        total_norm += param_norm ** 2
    total_norm = total_norm ** (1. / 2)
    return total_norm

def collate_fn(batch):
    if isinstance(batch, np.ndarray) or (isinstance(batch, list) and isinstance(batch[0], np.ndarray)):
        return ltensor(batch).contiguous()
    else:
        return torch.stack(batch).contiguous()

def mask_fairDiscriminators(discriminators, mask):
    # compress('ABCDEF', [1,0,1,0,1,1]) --> A C E F
    return (d for d, s in zip(discriminators, mask) if s)

def apply_filters(args,p_lhs_emb,p_rhs_emb,nce_lhs_emb,nce_rhs_emb,\
        rel_emb,p_batch_var,nce_batch,d_outs):

    filter_l_emb, filter_r_emb = 0,0
    filter_nce_l_emb, filter_nce_r_emb = 0,0
    if args.sample_mask:
        for filter_ in masked_filter_set:
            if filter_ is not None:
                filter_l_emb += filter_(p_lhs_emb)
                filter_r_emb += filter_(p_rhs_emb)
                filter_nce_l_emb += filter_(nce_lhs_emb)
                filter_nce_r_emb += filter_(nce_rhs_emb)
        p_enrgs = (filter_l_emb + rel_emb[:len(p_batch_var)] -\
                filter_r_emb).norm(p=self.p, dim=1)
        nce_enrgs = (filter_nce_l_emb + rel_emb[len(p_batch_var):(len(p_batch_var)+len(nce_batch))] -\
                filter_nce_r_emb).norm(p=self.p, dim=1)
    else:
        filter_l_emb = p_lhs_emb
        filter_r_emb = p_rhs_emb
        filter_nce_l_emb = nce_lhs_emb
        filter_nce_r_emb = nce_rhs_emb
        p_enrgs = d_outs[:len(p_batch_var)]
        nce_enrgs = d_outs[len(p_batch_var):(len(p_batch_var)+len(nce_batch))]

    return p_enrgs, nce_enrgs, filter_l_emb

def train(data_loader, counter, args, train_hash, modelD, optimizerD,\
         fairD_set, optimizer_fairD_set, filter_set, experiment):

    lossesD = []
    monitor_grads = []
    total_ent = 0
    fairD_gender_loss,fairD_occupation_loss,fairD_age_loss,\
            fairD_random_loss = 0,0,0,0
    loss_func = MarginRankingLoss(args.margin)

    if args.show_tqdm:
        data_itr = tqdm(enumerate(data_loader))
    else:
        data_itr = enumerate(data_loader)

    for idx, p_batch in data_itr:
        ''' Sample Fairness Discriminators '''
        if args.sample_mask:
            mask = np.random.choice([0, 1], size=(3,))
            masked_fairD_set = list(mask_fairDiscriminators(fairD_set,mask))
            masked_optimizer_fairD_set = list(mask_fairDiscriminators(optimizer_fairD_set,mask))
            masked_filter_set = list(mask_fairDiscriminators(filter_set,mask))
        else:
            ''' No mask applied despite the name '''
            masked_fairD_set = fairD_set
            masked_optimizer_fairD_set = optimizer_fairD_set
            masked_filter_set = filter_set

        nce_batch, q_samples = corrupt_batch(p_batch,args.num_ent,\
                args.num_users, args.num_movies)

        if args.filter_false_negs:
            if args.prefetch_to_gpu:
                nce_np = nce_batch.cpu().numpy()
            else:
                nce_np = nce_batch.numpy()

            nce_falseNs = ftensor(np.array([int(x.tobytes() in train_hash) for x in nce_np], dtype=np.float32))
            nce_falseNs = Variable(nce_falseNs.cuda()) if args.use_cuda else Variable(nce_falseNs)
        else:
            nce_falseNs = None

        if args.use_cuda:
            p_batch = p_batch.cuda()
            nce_batch = nce_batch.cuda()
            q_samples = q_samples.cuda()

        p_batch_var = Variable(p_batch)
        nce_batch = Variable(nce_batch)
        q_samples = Variable(q_samples)

        ''' Number of Active Discriminators '''
        constant = len(masked_fairD_set) - masked_fairD_set.count(None)
        d_ins = torch.cat([p_batch_var, nce_batch], dim=0).contiguous()
        ''' Update TransD Model '''
        if constant != 0:
            d_outs,lhs_emb,rhs_emb,rel_emb = modelD(d_ins,True)
            p_lhs_emb = lhs_emb[:len(p_batch_var)]
            p_rhs_emb = rhs_emb[:len(p_batch_var)]
            nce_lhs_emb = lhs_emb[len(p_batch_var):(len(p_batch_var)+len(nce_batch))]
            nce_rhs_emb = rhs_emb[len(p_batch_var):(len(p_batch_var)+len(nce_batch))]
            l_penalty = 0

            ''' Apply Filter or Not to Embeddings '''
            p_enrgs,nce_enrgs,filter_l_emb = apply_filters(args,p_lhs_emb,p_rhs_emb,nce_lhs_emb,\
                    nce_rhs_emb,rel_emb,p_batch_var,nce_batch,d_outs)

            ''' Apply Discriminators '''
            for fairD_disc, fair_optim in zip(masked_fairD_set,masked_optimizer_fairD_set):
                if fairD_disc is not None and fair_optim is not None:
                    fair_optim.zero_grad()
                    l_penalty += fairD_disc(filter_l_emb,p_batch[:,0])
                    if not args.use_cross_entropy:
                        fairD_loss = -1*(1 - l_penalty)
                    else:
                        fairD_loss = l_penalty
                    fairD_loss.backward(retain_graph=True)
                    fair_optim.step()

            if not args.use_cross_entropy:
                fair_penalty = constant - l_penalty
            else:
                fair_penalty = -1*l_penalty

            if not args.freeze_transD:
                optimizerD.zero_grad()
                nce_term, nce_term_scores = loss_func(p_enrgs, nce_enrgs, weights=(1.-nce_falseNs))
                lossD = nce_term + args.gamma*fair_penalty
                lossD.backward(retain_graph=True)
                optimizerD.step()

        # else:
            # d_outs = modelD(d_ins)
            # fair_penalty = Variable(torch.zeros(1)).cuda()
            # p_enrgs = d_outs[:len(p_batch_var)]
            # nce_enrgs = d_outs[len(p_batch_var):(len(p_batch_var)+len(nce_batch))]

        if constant != 0:
            correct = 0
            gender_correct,occupation_correct,age_correct,random_correct = 0,0,0,0
            precision_list = []
            recall_list = []
            fscore_list = []
            correct = 0
            for fairD_disc, fair_optim in zip(masked_fairD_set,masked_optimizer_fairD_set):
                if fairD_disc is not None and fair_optim is not None:
                    fair_optim.zero_grad()
                    ''' No Gradients Past Here '''
                    with torch.no_grad():
                        d_outs,lhs_emb,rhs_emb,rel_emb = modelD(d_ins,True)
                        p_lhs_emb = lhs_emb[:len(p_batch)]

                        ''' Apply Filter or Not to Embeddings '''
                        if args.sample_mask or args.use_trained_filters:
                            filter_emb = 0
                            for filter_ in masked_filter_set:
                                if filter_ is not None:
                                    filter_emb += filter_(p_lhs_emb)
                        else:
                            filter_emb = p_lhs_emb
                        l_preds, l_A_labels, probs = fairD_disc.predict(filter_emb,p_batch[:,0],return_preds=True)
                        l_correct = l_preds.eq(l_A_labels.view_as(l_preds)).sum().item()
                        if fairD_disc.attribute == 'gender':
                            fairD_gender_loss = fairD_loss.detach().cpu().numpy()
                            l_precision,l_recall,l_fscore,_ = precision_recall_fscore_support(l_A_labels, l_preds,\
                                    average='binary')
                            gender_correct += l_correct #
                        elif fairD_disc.attribute == 'occupation':
                            fairD_occupation_loss = fairD_loss.detach().cpu().numpy()
                            l_precision,l_recall,l_fscore,_ = precision_recall_fscore_support(l_A_labels, l_preds,\
                                    average='micro')
                            occupation_correct += l_correct
                        elif fairD_disc.attribute == 'age':
                            fairD_age_loss = fairD_loss.detach().cpu().numpy()
                            l_precision,l_recall,l_fscore,_ = precision_recall_fscore_support(l_A_labels, l_preds,\
                                    average='micro')
                            age_correct += l_correct
                        else:
                            fairD_random_loss = fairD_loss.detach().cpu().numpy()
                            l_precision,l_recall,l_fscore,_ = precision_recall_fscore_support(l_A_labels, l_preds,\
                                    average='micro')
                            random_correct += l_correct

    ''' Logging for end of epoch '''
    if args.do_log:
        if not args.freeze_transD:
            experiment.log_metric("TransD Loss",float(lossD),step=counter)
        if fairD_set[0] is not None:
            experiment.log_metric("Fair Gender Disc Loss",float(fairD_gender_loss),step=counter)
        if fairD_set[1] is not None:
            experiment.log_metric("Fair Occupation Disc Loss",float(fairD_occupation_loss),step=counter)
        if fairD_set[2] is not None:
            experiment.log_metric("Fair Age Disc Loss",float(fairD_age_loss),step=counter)
        if fairD_set[3] is not None:
            experiment.log_metric("Fair Random Disc Loss",float(fairD_age_loss),step=counter)

def test_fairness(dataset,args,modelD,experiment,fairD,\
        attribute,epoch,filter_=None,retrain=False):

    test_loader = DataLoader(dataset, num_workers=1, batch_size=4096, collate_fn=collate_fn)
    correct = 0
    total_ent = 0
    precision_list = []
    recall_list = []
    fscore_list = []
    preds_list = []
    labels_list = []

    if args.show_tqdm:
        data_itr = tqdm(enumerate(test_loader))
    else:
        data_itr = enumerate(test_loader)

    for idx, triplet in data_itr:
        lhs, rel, rhs = triplet[:,0], triplet[:,1],triplet[:,2]
        l_batch = Variable(lhs).cuda()
        r_batch = Variable(rhs).cuda()
        rel_batch = Variable(rel).cuda()
        lhs_emb = modelD.get_embed(l_batch,rel_batch)

        if filter_ is not None:
            lhs_emb = filter_(lhs_emb)

        l_preds,l_A_labels,probs = fairD.predict(lhs_emb,lhs,return_preds=True)
        l_correct = l_preds.eq(l_A_labels.view_as(l_preds)).sum().item()
        if attribute == 'gender':
            l_precision,l_recall,l_fscore,_ = precision_recall_fscore_support(l_A_labels, l_preds,\
                    average='binary')
        else:
            l_precision,l_recall,l_fscore,_ = precision_recall_fscore_support(l_A_labels, l_preds,\
                    average='micro')

        precision = l_precision
        recall = l_recall
        fscore = l_fscore
        precision_list.append(precision)
        recall_list.append(recall)
        fscore_list.append(fscore)
        preds_list.append(probs)
        labels_list.append(l_A_labels.view_as(l_preds))
        correct += l_correct
        total_ent += len(lhs_emb)

    if args.do_log:
        acc = 100. * correct / total_ent
        mean_precision = np.mean(np.asarray(precision_list))
        mean_recall = np.mean(np.asarray(recall_list))
        mean_fscore = np.mean(np.asarray(fscore_list))
        preds_list = torch.cat(preds_list,0).data.cpu().numpy()
        labels_list = torch.cat(labels_list,0).data.cpu().numpy()
        if retrain:
            attribute = 'Retrained_D_' + attribute
        experiment.log_metric(attribute + "_Valid FairD Accuracy",float(acc),step=epoch)
        if attribute == 'gender' or attribute == 'random':
            AUC = roc_auc_score(labels_list, preds_list)
            experiment.log_metric(attribute + "_Valid FairD AUC",float(AUC),step=epoch)

def test(dataset, args, all_hash, modelD, subsample=1):
    l_ranks, r_ranks = [], []
    test_loader = DataLoader(dataset, num_workers=1, collate_fn=collate_fn)

    cst_inds = np.arange(args.num_ent, dtype=np.int64)[:,None]
    if args.show_tqdm:
        data_itr = tqdm(enumerate(test_loader))
    else:
        data_itr = enumerate(test_loader)

    for idx, triplet in data_itr:
        if idx % subsample != 0:
            continue

        lhs, rel, rhs = triplet.view(-1)

        l_batch = np.concatenate([cst_inds, np.array([[rel, rhs]]).repeat(args.num_ent, axis=0)], axis=1)
        r_batch = np.concatenate([np.array([[lhs, rel]]).repeat(args.num_ent, axis=0), cst_inds], axis=1)

        l_fns = np.array([int(x.tobytes() in all_hash) for x in l_batch], dtype=np.float32)
        r_fns = np.array([int(x.tobytes() in all_hash) for x in r_batch], dtype=np.float32)

        l_batch = ltensor(l_batch).contiguous()
        r_batch = ltensor(r_batch).contiguous()

        if args.use_cuda:
            l_batch = l_batch.cuda()
            r_batch = r_batch.cuda()

        l_batch = Variable(l_batch)
        r_batch = Variable(r_batch)

        d_ins = torch.cat([l_batch, r_batch], dim=0)
        d_outs = modelD(d_ins)
        l_enrgs = d_outs[:len(l_batch)]
        r_enrgs = d_outs[len(l_batch):]

        l_rank = compute_rank(v2np(l_enrgs), lhs, mask_observed=l_fns)
        r_rank = compute_rank(v2np(r_enrgs), rhs, mask_observed=r_fns)

        l_ranks.append(l_rank)
        r_ranks.append(r_rank)

    l_ranks = np.array(l_ranks)
    r_ranks = np.array(r_ranks)
    l_mean = l_ranks.mean()
    r_mean = r_ranks.mean()
    l_mrr = (1. / l_ranks).mean()
    r_mrr = (1. / r_ranks).mean()
    l_h10 = (l_ranks <= 10).mean()
    r_h10 = (r_ranks <= 10).mean()
    l_h5 = (l_ranks <= 5).mean()
    r_h5 = (r_ranks <= 5).mean()
    avg_mr = (l_mean + r_mean)/2
    avg_mrr = (l_mrr+r_mrr)/2
    avg_h10 = (l_h10+r_h10)/2
    avg_h5 = (l_h5+r_h5)/2

    return l_ranks, r_ranks, avg_mr, avg_mrr, avg_h10, avg_h5

def retrain_disc(args,train_loader,train_hash,test_set,modelD,optimizerD,\
        experiment,gender_filter,occupation_filter,age_filter,attribute):

    if args.use_trained_filters:
        print("Retrain New Discriminator with Filter on %s" %(attribute))
    else:
        print("Retrain New Discriminator on %s" %(attribute))

    ''' Reset some flags '''
    args.use_cross_entropy = True
    args.sample_mask = False
    args.freeze_transD = True
    new_fairD_gender,new_fairD_occupation,new_fairD_age,new_fairD_random = None,None,None,None
    new_optimizer_fairD_gender,new_optimizer_fairD_occupation,\
            new_optimizer_fairD_age,new_optimizer_fairD_random = None,None,None,None

    if attribute == 'gender':
        args.use_gender_attr = True
        args.use_occ_attr = False
        args.use_age_attr = False
        args.use_random_attr = False
        args.use_attr = False
    elif attribute =='occupation':
        args.use_gender_attr = False
        args.use_occ_attr = True
        args.use_age_attr = False
        args.use_random_attr = False
        args.use_attr = False
    elif attribute =='age':
        args.use_gender_attr = False
        args.use_occ_attr = False
        args.use_age_attr = True
        args.use_random_attr = False
        args.use_attr = False
    elif attribute =='random':
        args.use_gender_attr = False
        args.use_occ_attr = False
        args.use_age_attr = False
        args.use_random_attr = True
        args.use_attr = False
    else:
        args.use_gender_attr = False
        args.use_occ_attr = False
        args.use_age_attr = False
        args.use_random_attr = False
        args.use_attr = True

    '''Retrain Discriminator on Frozen TransD Model '''
    if args.use_occ_attr:
        attr_data = [args.users,args.movies]
        new_fairD_occupation = DemParDisc(args.embed_dim,attr_data,\
                attribute='occupation',use_cross_entropy=args.use_cross_entropy)
        new_fairD_occupation.cuda()
        new_optimizer_fairD_occupation = optimizer(new_fairD_occupation.parameters(),'adam',args.lr)
        fairD_disc = new_fairD_occupation
        fair_optim = new_optimizer_fairD_occupation
    elif args.use_gender_attr:
        attr_data = [args.users,args.movies]
        new_fairD_gender = DemParDisc(args.embed_dim,attr_data,use_cross_entropy=args.use_cross_entropy)
        # new_fairD_gender.load(args.outname_base+'GenderFairD_final.pts')
        new_optimizer_fairD_gender = optimizer(new_fairD_gender.parameters(),'adam',args.lr)
        new_fairD_gender.cuda()
        fairD_disc = new_fairD_gender
        fair_optim = new_optimizer_fairD_gender
    elif args.use_age_attr:
        attr_data = [args.users,args.movies]
        new_fairD_age = DemParDisc(args.embed_dim,attr_data,\
            attribute='age',use_cross_entropy=args.use_cross_entropy)
        new_optimizer_fairD_age = optimizer(new_fairD_age.parameters(),'adam',args.lr)
        new_fairD_age.cuda()
        fairD_disc = new_fairD_age
        fair_optim = new_optimizer_fairD_age
    elif args.use_random_attr:
        attr_data = [args.users,args.movies]
        new_fairD_random = DemParDisc(args.embed_dim,attr_data,\
                attribute='random',use_cross_entropy=args.use_cross_entropy)
        new_optimizer_fairD_random = optimizer(new_fairD_random.parameters(),'adam',args.lr)
        new_fairD_random.cuda()
        fairD_disc = new_fairD_random
        fair_optim = new_optimizer_fairD_random

    attr_data = [args.users,args.movies]
    new_fairD_set = [new_fairD_gender,new_fairD_occupation,new_fairD_age,new_fairD_random]
    new_optimizer_fairD_set = [new_optimizer_fairD_gender,new_optimizer_fairD_occupation,\
            new_optimizer_fairD_age,new_optimizer_fairD_random]
    if args.use_trained_filters:
        filter_set = [gender_filter,occupation_filter,age_filter,None]
    else:
        filter_set = [None,None,None,None]

    # test_fairness(test_set,args, modelD,experiment,\
            # new_fairD_gender, attribute='gender',epoch=0,\
            # retrain=True)
    ''' Freeze Model + Filters '''
    for filter_ in filter_set:
        if filter_ is not None:
            freeze_model(filter_)
    freeze_model(modelD)

    with experiment.test():
        for epoch in tqdm(range(1, args.num_epochs + 1)):
            train(train_loader,epoch,args,train_hash,modelD,optimizerD,\
                    new_fairD_set,new_optimizer_fairD_set,filter_set,experiment)
            gc.collect()
            if epoch % args.valid_freq == 0:
                if args.use_attr:
                    test_fairness(test_set,args, modelD,experiment,\
                            new_fairD_gender, attribute='gender',epoch=epoch,\
                            retrain=True)
                    test_fairness(test_set,args,modelD,experiment,\
                            new_fairD_occupation,attribute='occupation',epoch=epoch,\
                            retrain=True)
                    test_fairness(test_set,args, modelD,experiment,\
                            new_fairD_age,attribute='age',epoch=epoch,retrain=True)
                elif args.use_gender_attr:
                    test_fairness(test_set,args,modelD,experiment,\
                            new_fairD_gender, attribute='gender',epoch=epoch,\
                            retrain=True)
                elif args.use_occ_attr:
                    test_fairness(test_set,args,modelD,experiment,\
                            new_fairD_occupation,attribute='occupation',epoch=epoch,\
                            retrain=True)
                elif args.use_age_attr:
                    test_fairness(test_set,args,modelD,experiment,\
                            new_fairD_age,attribute='age',epoch=epoch,retrain=True)
                elif args.use_random_attr:
                    test_fairness(test_set,args,modelD,experiment,\
                            new_fairD_random,attribute='random',epoch=epoch,retrain=True)
