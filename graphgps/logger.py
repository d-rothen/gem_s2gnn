import logging
import time

import numpy as np
import torch
from scipy.stats import stats
from sklearn.metrics import accuracy_score, precision_score, recall_score, \
    f1_score, roc_auc_score, mean_absolute_error, mean_squared_error, \
    confusion_matrix
from sklearn.metrics import r2_score
from torch_geometric.graphgym import get_current_gpu_usage
from torch_geometric.graphgym.config import cfg
from torch_geometric.graphgym.logger import infer_task, Logger
from torch_geometric.graphgym.utils.io import dict_to_json, dict_to_tb
from torchmetrics.functional import auroc, accuracy, average_precision

import graphgps.metrics_ogb as metrics_ogb
from graphgps.metric_wrapper import MetricWrapper


def accuracy_SBM(targets, pred_int):
    """Accuracy eval for Benchmarking GNN's PATTERN and CLUSTER datasets.
    https://github.com/graphdeeplearning/benchmarking-gnns/blob/master/train/metrics.py#L34
    """
    S = targets
    C = pred_int
    CM = confusion_matrix(S, C).astype(np.float32)
    nb_classes = CM.shape[0]
    targets = targets.cpu().detach().numpy()
    nb_non_empty_classes = 0
    pr_classes = np.zeros(nb_classes)
    for r in range(nb_classes):
        cluster = np.where(targets == r)[0]
        if cluster.shape[0] != 0:
            pr_classes[r] = CM[r, r] / float(cluster.shape[0])
            if CM[r, r] > 0:
                nb_non_empty_classes += 1
        else:
            pr_classes[r] = 0.0
    acc = np.sum(pr_classes) / float(nb_classes)
    return acc


class CustomLogger(Logger):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Whether to run comparison tests of alternative score implementations.
        self.test_scores = False

    def reset(self):
        super().reset()
        self._data = []

    # basic properties
    def basic(self):
        stats = {
            'loss': round(self._loss / self._size_current, max(8, cfg.round)),
            'lr': round(self._lr, max(8, cfg.round)),
            'params': self._params,
            'time_iter': round(self.time_iter(), cfg.round),
        }
        gpu_memory = get_current_gpu_usage()
        if gpu_memory > 0:
            stats['gpu_memory'] = gpu_memory
        return stats

    # task properties
    def classification_binary(self):
        true = torch.cat(self._true).squeeze(-1)
        pred_score = torch.cat(self._pred)
        pred_int = self._get_pred_int(pred_score)

        if true.shape[0] < 1e7:  # AUROC computation for very large datasets is too slow.
            # TorchMetrics AUROC on GPU if available.
            auroc_score = auroc(pred_score.to(torch.device(cfg.device)),
                                true.to(torch.device(cfg.device)),
                                pos_label=1)
            if self.test_scores:
                # SK-learn version.
                try:
                    r_a_score = roc_auc_score(true.cpu().numpy(),
                                              pred_score.cpu().numpy())
                except ValueError:
                    r_a_score = 0.0
                assert np.isclose(float(auroc_score), r_a_score)
        else:
            auroc_score = 0.

        reformat = lambda x: round(float(x), cfg.round)
        res = {
            'accuracy': reformat(accuracy_score(true, pred_int)),
            'precision': reformat(precision_score(true, pred_int)),
            'recall': reformat(recall_score(true, pred_int)),
            'f1': reformat(f1_score(true, pred_int)),
            'auc': reformat(auroc_score),
        }
        if cfg.metric_best == 'accuracy-SBM':
            res['accuracy-SBM'] = reformat(accuracy_SBM(true, pred_int))
        return res

    def classification_multi(self):
        true, pred_score = torch.cat(self._true), torch.cat(self._pred)
        pred_int = self._get_pred_int(pred_score)
        reformat = lambda x: round(float(x), cfg.round)

        res = {
            'accuracy': reformat(accuracy_score(true, pred_int)),
            'f1': reformat(f1_score(true, pred_int,
                                    average='macro', zero_division=0)),
        }
        if cfg.metric_best == 'accuracy-SBM':
            res['accuracy-SBM'] = reformat(accuracy_SBM(true, pred_int))
        if true.shape[0] < 1e7:
            # AUROC computation for very large datasets runs out of memory.
            # TorchMetrics AUROC on GPU is much faster than sklearn for large ds
            res['auc'] = reformat(auroc(pred_score.to(torch.device(cfg.device)),
                                        true.to(torch.device(cfg.device)).squeeze(),
                                        num_classes=pred_score.shape[1],
                                        average='macro'))

            if self.test_scores:
                # SK-learn version.
                sk_auc = reformat(roc_auc_score(true, pred_score.exp(),
                                                average='macro',
                                                multi_class='ovr'))
                assert np.isclose(sk_auc, res['auc'])

        return res

    def classification_multilabel(self):
        true, pred_score = torch.cat(self._true), torch.cat(self._pred)
        reformat = lambda x: round(float(x), cfg.round)

        # Send to GPU to speed up TorchMetrics if possible.
        true = true.to(torch.device(cfg.device))
        pred_score = pred_score.to(torch.device(cfg.device))
        
        num_labels = pred_score.shape[-1]

        # acc = MetricWrapper(metric='accuracy',
        #                     target_nan_mask='ignore-mean-label',
        #                     threshold=0.5,
        #                     cast_to_int=True, task="multilabel", num_labels=num_labels)
        # ap = MetricWrapper(metric='averageprecision',
        #                    target_nan_mask='ignore-mean-label',
        #                 #    pos_label=1,
        #                    cast_to_int=True, task="multilabel", num_labels=num_labels)
        # auroc = MetricWrapper(metric='auroc',
        #                       target_nan_mask='ignore-mean-label',
        #                     #   pos_label=1,
        #                       cast_to_int=True, task="multilabel", num_labels=num_labels)

        acc_score = accuracy(pred_score, true.int(), task="multilabel", num_labels=num_labels)
        ap_score = average_precision(pred_score, true.int(), task="multilabel", num_labels=num_labels)
        auc_score = auroc(pred_score, true.int(), task="multilabel", num_labels=num_labels)
        
        results = {
            'accuracy': reformat(acc_score),
            'ap': reformat(ap_score),
            'auc': reformat(auc_score),
        }

        if self.test_scores:
            # Compute metric by OGB Evaluator methods.
            true = true.cpu().numpy()
            pred_score = pred_score.cpu().numpy()
            ogb = {
                'accuracy': reformat(metrics_ogb.eval_acc(
                    true, (pred_score > 0.).astype(int))['acc']),
                'ap': reformat(metrics_ogb.eval_ap(true, pred_score)['ap']),
                'auc': reformat(
                    metrics_ogb.eval_rocauc(true, pred_score)['rocauc']),
            }
            assert np.isclose(ogb['accuracy'], results['accuracy'])
            assert np.isclose(ogb['ap'], results['ap'])
            assert np.isclose(ogb['auc'], results['auc'])

        return results

    def subtoken_prediction(self):
        from ogb.graphproppred import Evaluator
        evaluator = Evaluator('ogbg-code2')

        seq_ref_list = []
        seq_pred_list = []
        for seq_pred, seq_ref in zip(self._pred, self._true):
            seq_ref_list.extend(seq_ref)
            seq_pred_list.extend(seq_pred)

        input_dict = {"seq_ref": seq_ref_list, "seq_pred": seq_pred_list}
        result = evaluator.eval(input_dict)
        result['f1'] = result['F1']
        del result['F1']
        return result

    def ranking(self, suffix='', ids=None):
        reformat = lambda x: round(float(x), cfg.round)
        opas = []
        corrs = []
        one_minus_slowdown = []
        kendal_taus = []
        err1 = []
        err3 = []
        err5 = []
        err10 = []
        _true, _pred = self._true, self._pred
        if ids is not None:
            _true = [e[None] for elems in _true for e in elems]
            _true = [_true[id_] for id_ in ids]
            _pred = [e[None] for elems in _pred for e in elems]
            _pred = [_pred[id_] for id_ in ids]
        for true, pred in zip(_true, _pred):
            true = true.numpy()
            pred = pred.numpy()
            for i in range(true.shape[0]):
                if cfg.dataset.name == 'TPUGraphs' and cfg.dataset.tpu_graphs.tpu_task == 'layout':
                    opas.append(eval_opa(true[i], pred[i]))
                corrs.append(eval_spearmanr(true[i], pred[i])['spearmanr'])
                one_minus_slowdown.append(
                    eval_one_minus_slowdown(true[i], pred[i]))
                kendal_taus.append(eval_kendal_tau(true[i], pred[i]))
                err1.append(eval_err_top_k(true[i], pred[i], 1))
                err3.append(eval_err_top_k(true[i], pred[i], 3))
                err5.append(eval_err_top_k(true[i], pred[i], 5))
                err10.append(eval_err_top_k(true[i], pred[i], 10))
        result = {
            f'spearmanr{suffix}': reformat(np.mean(corrs)),
            f'one_minus_slowdown{suffix}': reformat(np.mean(one_minus_slowdown)),
            f'kendal_tau{suffix}': reformat(np.mean(kendal_taus)),
            f'err1{suffix}': reformat(np.mean(err1)),
            f'err3{suffix}': reformat(np.mean(err3)),
            f'err5{suffix}': reformat(np.mean(err5)),
            f'err10{suffix}': reformat(np.mean(err10))
        }
        if cfg.dataset.name == 'TPUGraphs' and cfg.dataset.tpu_graphs.tpu_task == 'layout':
            result[f'opa{suffix}'] = reformat(np.mean(opas))
        return result

    def regression(self):
        true, pred = torch.cat(self._true), torch.cat(self._pred)
        reformat = lambda x: round(float(x), cfg.round)
        return {
            'mae': reformat(mean_absolute_error(true, pred)),
            'r2': reformat(r2_score(true, pred, multioutput='uniform_average')),
            'spearmanr': reformat(eval_spearmanr(true.numpy(),
                                                 pred.numpy())['spearmanr']),
            'mse': reformat(mean_squared_error(true, pred)),
            # 'rmse': reformat(mean_squared_error(true, pred, squared=False)),
        }

    def update_stats(self, pred, true, loss, lr, time_used, params,
                     data=None, dataset_name=None, **kwargs):
        if dataset_name == 'ogbg-code2':
            assert true['y_arr'].shape[1] == len(pred)  # max_seq_len (5)
            assert true['y_arr'].shape[0] == pred[0].shape[0]  # batch size
            batch_size = true['y_arr'].shape[0]

            # Decode the predicted sequence tokens, so we don't need to store
            # the logits that take significant memory.
            from graphgps.loader.ogbg_code2_utils import idx2vocab, \
                decode_arr_to_seq
            arr_to_seq = lambda arr: decode_arr_to_seq(arr, idx2vocab)
            mat = []
            for i in range(len(pred)):
                mat.append(torch.argmax(pred[i].detach(), dim=1).view(-1, 1))
            mat = torch.cat(mat, dim=1)
            seq_pred = [arr_to_seq(arr) for arr in mat]
            seq_ref = [true['y'][i] for i in range(len(true['y']))]
            pred = seq_pred
            true = seq_ref
        else:
            assert true.shape[0] == pred.shape[0]
            batch_size = true.shape[0]
        self._iter += 1
        self._true.append(true)
        self._pred.append(pred)
        if data is not None:
            data.update({'pred': pred})
            self._data.append(data)
        self._size_current += batch_size
        self._loss += loss * batch_size
        self._lr = lr
        self._params = params
        self._time_used += time_used
        self._time_total += time_used
        for key, val in kwargs.items():
            if key not in self._custom_stats:
                self._custom_stats[key] = val * batch_size
            else:
                self._custom_stats[key] += val * batch_size

    def write_epoch(self, cur_epoch, splits: dict = None):
        start_time = time.perf_counter()
        basic_stats = self.basic()

        if self.task_type == 'regression':
            task_stats = self.regression()
        elif self.task_type == 'classification_binary':
            task_stats = self.classification_binary()
        elif self.task_type == 'classification_multi':
            task_stats = self.classification_multi()
        elif self.task_type == 'classification_multilabel':
            task_stats = self.classification_multilabel()
        elif self.task_type == 'subtoken_prediction':
            task_stats = self.subtoken_prediction()
        elif self.task_type == 'ranking':
            task_stats = self.ranking()
            if splits is not None:
                for suffix, ids in splits.items():
                    task_stats.update(self.ranking(suffix, ids))
        else:
            raise ValueError('Task has to be regression or classification')

        epoch_stats = {'epoch': cur_epoch,
                       'time_epoch': round(self._time_used, cfg.round)}
        eta_stats = {'eta': round(self.eta(cur_epoch), cfg.round),
                     'eta_hours': round(self.eta(cur_epoch) / 3600, cfg.round)}
        custom_stats = self.custom()

        if self.name == 'train':
            stats = {
                **epoch_stats,
                **eta_stats,
                **basic_stats,
                **task_stats,
                **custom_stats
            }
        else:
            stats = {
                **epoch_stats,
                **basic_stats,
                **task_stats,
                **custom_stats
            }

        # print
        logging.info('{}: {}'.format(self.name, stats))
        # json
        dict_to_json(stats, '{}/stats.json'.format(self.out_dir))
        # tensorboard
        if cfg.tensorboard_each_run:
            dict_to_tb(stats, self.tb_writer, cur_epoch)
        self.reset()
        if cur_epoch < 3:
            logging.info(f"...computing epoch stats took: "
                         f"{time.perf_counter() - start_time:.2f}s")
        return stats


def create_logger():
    """
    Create logger for the experiment

    Returns: List of logger objects

    """
    loggers = []
    names = ['train', 'val', 'test']
    for i, dataset in enumerate(range(cfg.share.num_splits)):
        loggers.append(CustomLogger(name=names[i], task_type=infer_task()))
    return loggers


def eval_spearmanr(y_true, y_pred):
    """Compute Spearman Rho averaged across tasks.
    """
    res_list = []

    if y_true.ndim == 1:
        res_list.append(stats.spearmanr(y_true, y_pred)[0])
    else:
        for i in range(y_true.shape[1]):
            # ignore nan values
            is_labeled = ~np.isnan(y_true[:, i])
            res_list.append(stats.spearmanr(y_true[is_labeled, i],
                                            y_pred[is_labeled, i])[0])

    return {'spearmanr': sum(res_list) / len(res_list)}

def eval_opa(y_true, y_pred):
    num_preds = y_pred.shape[0]
    i_idx = torch.arange(num_preds).repeat(num_preds)
    j_idx = torch.arange(num_preds).repeat_interleave(num_preds)
    pairwise_true = y_true[i_idx] > y_true[j_idx]
    opa_indices = pairwise_true.nonzero()[0].flatten()
    opa_preds = y_pred[i_idx[opa_indices]] - y_pred[j_idx[opa_indices]]
    opa_acc = float((opa_preds > 0).sum()) / (opa_preds.shape[0] + 1e-10)
    return opa_acc


def eval_one_minus_slowdown(y_true, y_pred, k=5):
    k = min(y_pred.shape[0] - 1, k)
    pred_runtime = y_true[np.argpartition(y_pred, k)[:k]]
    return 2 - np.divide(pred_runtime.min(), y_true.min())


def eval_kendal_tau(y_true, y_pred):
    return stats.kendalltau(y_pred, y_true).correlation


def eval_err_top_k(y_true, y_pred, k=5):
    if y_true.shape[0] <= k:
        pred_runtime = y_true.min()
    else:
        pred_runtime = y_true[np.argpartition(y_pred, k)[:k]].min()
    best_runtime = y_true.min()
    return np.divide(pred_runtime - best_runtime, best_runtime)


# def eval_err_top_k(y_true, y_pred, ks=[1, 3, 5, 10]):
#     k_max = ks[-1]
#     pred_runtime = y_true[np.argsort(y_pred)[:k_max]]
#     best_runtime = y_true.min()
#     return [np.divide(pred_runtime[:k].min() - best_runtime, best_runtime)
#             for k in ks]

        # pred_rank = torch.argsort(pred, dim=-1, descending=False)
        # true_rank = torch.argsort(true, dim=-1, descending=False)
        # pred_rank = pred_rank.cpu().numpy()
        # true_rank = true_rank.cpu().numpy()
        # true = true.cpu().numpy()
        # err_1 = (true[pred_rank[0]] - true[true_rank[0]]) / true[true_rank[0]]
        # err_10 = (np.min(true[pred_rank[:10]]) - true[true_rank[0]]) / true[true_rank[0]]
        # err_100 = (np.min(true[pred_rank[:100]]) - true[true_rank[0]]) / true[true_rank[0]]
        # print('top 1 err: ' + str(err_1))
        # print('top 10 err: ' + str(err_10))
        # print('top 100 err: ' + str(err_100))
        # print("kendall:" + str(scipy.stats.kendalltau(pred_rank, true_rank).correlation))