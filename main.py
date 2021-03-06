import sys
import os
import argparse
import numpy as np
import torch
from data.synthetic_dataset import create_synthetic_dataset, SyntheticDataset
from models.base_models import EncoderRNN, DecoderRNN, Net_GRU, NetFullyConnected, get_base_model
from models.index_models import get_index_model
from loss.dilate_loss import dilate_loss
from train import train_model, get_optimizer, train_index_model
from eval import eval_base_model, eval_inf_model, eval_inf_index_model
from torch.utils.data import DataLoader
import random
from tslearn.metrics import dtw, dtw_path
import matplotlib.pyplot as plt
import warnings
import warnings; warnings.simplefilter('ignore')
import json
from torch.utils.tensorboard import SummaryWriter
import shutil
import properscoring as ps

from ray import tune
from ray.tune import CLIReporter
from ray.tune.schedulers import ASHAScheduler
from functools import partial

from models import inf_models, inf_index_models
import utils

os.environ["TUNE_GLOBAL_CHECKPOINT_S"] = "1000000"

random.seed(0)
torch.manual_seed(0)
np.random.seed(0)

parser = argparse.ArgumentParser()

parser.add_argument('dataset_name', type=str, help='dataset_name')
#parser.add_argument('model_name', type=str, help='model_name')

parser.add_argument('--N_input', type=int, default=20,
                    help='number of input steps')
parser.add_argument('--N_output', type=int, default=20,
                    help='number of output steps')

parser.add_argument('--output_dir', type=str,
                    help='Path to store all raw outputs', default='Outputs')
parser.add_argument('--saved_models_dir', type=str,
                    help='Path to store all saved models', default='saved_models')
parser.add_argument('--ignore_ckpt', action='store_true', default=False,
                    help='Start the training without loading the checkpoint')
parser.add_argument('--normalize', type=str, default='same',
                    help='Normalization type (avg, avg_per_series, quantile90, std)')

parser.add_argument('--epochs', type=int, default=500,
                    help='number of training epochs')
parser.add_argument('--print_every', type=int, default=50,
                    help='Print test output after every print_every epochs')
parser.add_argument('--learning_rate', type=float, default=0.001,# nargs='+',
                   help='Learning rate for the training algorithm')

parser.add_argument('-hls', '--hidden_size', type=int, default=128, nargs='+',
                   help='Number of units in RNN')
parser.add_argument('--num_grulstm_layers', type=int, default=1,# nargs='+',
                   help='Number of layers in RNN')
parser.add_argument('--fc_units', type=int, default=16, nargs='+',
                   help='Number of fully connected units on top of RNN state')
parser.add_argument('--batch_size', type=int, default=100,
                    help='Input batch size')
parser.add_argument('--gamma', type=float, default=0.01, nargs='+',
                   help='gamma parameter of DILATE loss')
parser.add_argument('--alpha', type=float, default=0.5,
                   help='alpha parameter of DILATE loss')
parser.add_argument('--teacher_forcing_ratio', type=float, default=0.5,
                   help='Probability of applying teacher forcing to a batch')
parser.add_argument('--deep_std', action='store_true', default=False,
                    help='Extra layers for prediction of standard deviation')
parser.add_argument('--train_twostage', action='store_true', default=False,
                    help='Train base model in two stages -- train only \
                          mean in first stage, train both in second stage')
parser.add_argument('--mse_loss_with_nll', action='store_true', default=False,
                    help='Add extra mse_loss when training with nll')
parser.add_argument('--second_moment', action='store_true', default=False,
                    help='compute std as std = second_moment - mean')
parser.add_argument('--variance_rnn', action='store_true', default=False,
                    help='Use second RNN to compute variance or variance related values')


parser.add_argument('--use_time_features', action='store_true', default=False,
                    help='Use time features derived from calendar-date')


# Hierarchical model arguments
parser.add_argument('--L', type=int, default=2,
                    help='number of levels in the hierarchy, leaves inclusive')
parser.add_argument('--K_list', type=int, nargs='*', default=[1],
                    help='List of bin sizes of each aggregation')
parser.add_argument('--learnK', action='store_true', default=False,
                    help='If True, aggregation splits are learned and \
                          Aggregate data contains dynamically generated \
                          piecewise linear functions')
parser.add_argument('--wavelet_levels', type=int, default=2,
                    help='number of levels of wavelet coefficients')
parser.add_argument('--fully_connected_agg_model', action='store_true', default=False,
                    help='If True, aggregate model will be a feed-forward network')
parser.add_argument('--transformer_agg_model', action='store_true', default=False,
                    help='If True, aggregate model will be a Transformer')

parser.add_argument('--plot_anecdotes', action='store_true', default=False,
                    help='Plot the comparison of various methods')
parser.add_argument('--save_agg_preds', action='store_true', default=False,
                    help='Save inputs, targets, and predictions of aggregate base models')

parser.add_argument('--device', type=str,
                    help='Device to run on', default='cpu')

# parameters for ablation study
parser.add_argument('--leak_agg_targets', action='store_true', default=False,
                    help='If True, aggregate targets are leaked to inference models')

# parameter to enable ray-tune
parser.add_argument('--no_ray_tune', action='store_true', default=False,
                    help='If True, ray tune is disabled for aggregate models')

#parser.add_argument('--patience', type=int, default=2,
#                    help='Number of epochs to wait for \
#                          before beginning cross-validation')
#parser.add_argument('--seed', type=int,
#                    help='Seed for parameter initialization',
#                    default=42)

args = parser.parse_args()

#args.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

args.base_model_names = [
#    'seq2seqdilate',
#    'seq2seqmse',
    'seq2seqnll',
#    'convmse'
]
args.inference_model_names = [
#    'DILATE',
#    'MSE',
    'NLL',
#    'NLLls',
#    'seq2seqmse_dualtpp',
    'seq2seqnll_dualtpp',
#    'seq2seqmse_optls',
#    'seq2seqnll_optls',
#    'seq2seqmse_optst',
    'seq2seqnll_optst',
#    'seq2seqmse_opttrend',
    'seq2seqnll_opttrend',
    'seq2seqnll_optklst',
    'seq2seqnll_optkls',
    'seq2seqnll_optklt',
#    'seq2seqmse_wavelet',
#    'seq2seqnll_wavelet',
]
args.aggregate_methods = [
    'sum',
#    'leastsquare',
#    'sumwithtrend',
    'slope',
#    'wavelet'
]

if 1 not in args.K_list:
    args.K_list = [1] + args.K_list

if args.learnK:
    # -1 is special level that denotes K will be leared from the data
    args.K_list = [1, -1]

if args.dataset_name in ['Traffic']:
    args.alpha = 0.8

if args.dataset_name in ['ECG5000']:
    args.teacher_forcing_ratio = 0.0

base_models = {}
base_models_preds = {}
index_models = {}
for name in args.base_model_names:
    base_models[name] = {}
    base_models_preds[name] = {}
    index_models[name] = {}
inference_models = {}
for name in args.inference_model_names:
    inference_models[name] = {}


os.makedirs(args.output_dir, exist_ok=True)
os.makedirs(args.saved_models_dir, exist_ok=True)

model2metrics = dict()
infmodel2preds = dict()


#dataset = utils.get_processed_data(args)
data_processor = utils.DataProcessor(args)
#level2data = dataset['level2data']
# ----- Start: base models training ----- #
for base_model_name in args.base_model_names:
    base_models[base_model_name] = {}
    base_models_preds[base_model_name] = {}
    index_models[base_model_name] = {}

    levels = args.K_list
    aggregate_methods = args.aggregate_methods
    if base_model_name in ['seq2seqdilate']:
        levels = [1]
        aggregate_methods = ['sum']

    for agg_method in aggregate_methods:
        base_models[base_model_name][agg_method] = {}
        base_models_preds[base_model_name][agg_method] = {}
        index_models[base_model_name][agg_method] = {}
        #level2data = dataset[agg_method]

        if agg_method in ['wavelet']:
            levels = list(range(1, args.wavelet_levels+3))

        for level in levels:
            level2data = data_processor.get_processed_data(args, agg_method, level)
            trainloader = level2data['trainloader']
            devloader = level2data['devloader']
            testloader = level2data['testloader']
            N_input = level2data['N_input']
            N_output = level2data['N_output']
            input_size = level2data['input_size']
            output_size = level2data['output_size']
            dev_norm = level2data['dev_norm']
            test_norm = level2data['test_norm']

            if base_model_name in ['seq2seqmse', 'seq2seqdilate', 'convmse']:
                point_estimates = True
            elif base_model_name in ['seq2seqnll']:
                point_estimates = False

            saved_models_dir = os.path.join(
                args.saved_models_dir,
                args.dataset_name+'_'+base_model_name+'_'+agg_method+'_'+str(level)
            )
            os.makedirs(saved_models_dir, exist_ok=True)
            writer = SummaryWriter(saved_models_dir)
            saved_models_path = os.path.join(saved_models_dir, 'state_dict_model.pt')
            output_dir = os.path.join(args.output_dir, base_model_name)
            os.makedirs(output_dir, exist_ok=True)
            print('\n {} {} {}'.format(base_model_name, agg_method, str(level)))

            #train_model(
            #    args, base_model_name, net_gru,
            #    trainloader, devloader, testloader, dev_norm,
            #    saved_models_path, output_dir, writer, eval_every=50, verbose=1
            #)

            # Create config dictionaries
            config_fixed = {
                'args': args,
                'agg_method': agg_method,
                'level': level,
                'base_model_name': base_model_name,
                'trainloader': trainloader,
                'devloader': devloader,
                'testloader': testloader,
                'dev_norm': dev_norm,
                'saved_models_path': saved_models_path,
                'output_dir': output_dir,
                'writer': writer,
                'eval_every': 50,
                'verbose': 1,
                'level': level,
                'N_input': N_input,
                'N_output': N_output,
                'input_size': input_size,
                'output_size': output_size,
                'point_estimates': point_estimates,
                'epochs': int(args.epochs * np.sqrt(level)) if level>1 else args.epochs,
            } # Fixed parameters
            config_tune = {
                'lr': tune.loguniform(1e-5, 1e-1),
                'hidden_size': tune.sample_from(lambda _: 2**np.random.randint(2, 8)),
            } # Tunable parameters
            # No tuning for level 1 models as of now
            if level == 1 or args.no_ray_tune:
                config_tune['lr'] = args.learning_rate
                config_tune['hidden_size'] = args.hidden_size
            config = {**config_fixed, **config_tune}


            # Create and train the network
            if level == -1:
                net_index = get_index_model(
                    args, config, level,
                    N_input, N_output, input_size, output_size,
                    point_estimates
                )
                config['net'] = net_index

                train_index_model(config)

                index_models[base_model_name][agg_method][level] = net_index
            else:
                net_gru = get_base_model(
                    args, config, base_model_name, level,
                    N_input, N_output, input_size, output_size,
                    point_estimates
                )
                config['net'] = net_gru
    
                if agg_method in ['leastsquare', 'sumwithtrend', 'slope', 'wavelet'] and level == 1:
                    base_models[base_model_name][agg_method][level] = base_models[base_model_name]['sum'][1]
                else:
                    train_model(config)
                    base_models[base_model_name][agg_method][level] = net_gru

                if args.learnK:
                    index_models[base_model_name][agg_method][level] \
                        = base_models[base_model_name][agg_method][level]


            writer.flush()

            if args.save_agg_preds and level>=1:
                testloader = level2data['testloader']
                test_norm = level2data['test_norm']
                print(agg_method, level, level2data['N_output'])
                (
                    dev_inputs, dev_target, pred_mu, pred_std,
                    metric_dilate, metric_mse, metric_dtw, metric_tdi,
                    metric_crps, metric_mae, metric_crps_part
                ) = eval_base_model(
                    args, base_model_name,
                    base_models[base_model_name][agg_method][level],
                    testloader, test_norm,
                    args.gamma, verbose=1
                )

                output_dir = os.path.join(args.output_dir, args.dataset_name + '_base')
                os.makedirs(output_dir, exist_ok=True)
                utils.write_aggregate_preds_to_file(
                    output_dir, base_model_name, agg_method, level,
                    utils.unnormalize(dev_inputs.detach().numpy(), test_norm),
                    dev_target.detach().numpy(),
                    pred_mu.detach().numpy(),
                    pred_std.detach().numpy()
                )

                # Aggregate level 1 predictions using current aggregation.
                base_models_preds[base_model_name][agg_method][level] = [pred_mu, pred_std]

                dev_target = dev_target.detach().numpy()
                pred_mu = pred_mu.detach().numpy()
                pred_std = pred_std.detach().numpy()
                pred_mu_bottom = base_models_preds[base_model_name][agg_method][1][0].detach().numpy()
                pred_std_bottom = base_models_preds[base_model_name][agg_method][1][1].detach().numpy()
                if level != 1:
                    if agg_method in ['slope']:
                        pred_mu_agg = utils.aggregate_seqs_slope(pred_mu_bottom, level, is_var=False)
                        pred_std_agg = np.sqrt(utils.aggregate_seqs_slope(pred_std_bottom**2, level, is_var=True))
                    elif agg_method in ['sum']:
                        pred_mu_agg = utils.aggregate_seqs_sum(pred_mu_bottom, level, is_var=False)
                        pred_std_agg = np.sqrt(utils.aggregate_seqs_sum(pred_std_bottom**2, level, is_var=True))
                        #import ipdb
                        #ipdb.set_trace()
                else:
                    pred_mu_agg = pred_mu_bottom
                    pred_std_agg = pred_std_bottom

                mae_agg = np.mean(np.abs(dev_target - pred_mu_agg))
                mae_base = np.mean(np.abs(dev_target - pred_mu))

                crps_agg = ps.crps_gaussian(
                    dev_target, mu=pred_mu_agg, sig=pred_std_agg
                ).mean()
                crps_base = ps.crps_gaussian(
                    dev_target, mu=pred_mu, sig=pred_std
                ).mean()

                if level!=1:
                    h_t = dev_inputs.shape[1]
                    n_e = dev_target.shape[1]
                    plt_dir = os.path.join(
                        output_dir, 'plots', agg_method,
                        'level_'+str(level),
                    )
                    os.makedirs(plt_dir, exist_ok=True)
                    for i in range(0, dev_inputs.shape[0]):
                        plt.plot(
                            np.arange(1, h_t+n_e+1),
                            np.concatenate([dev_inputs[i,:,0][-h_t:], dev_target[i,:,0]]),
                            'ko-'
                        )
                        plt.plot(np.arange(h_t+1, h_t+n_e+1), pred_mu[i,:,0], 'bo-')
                        plt.plot(np.arange(h_t+1, h_t+n_e+1), pred_mu_agg[i,:,0], 'ro-')
                        plt.savefig(
                            os.path.join(plt_dir, str(i)+'.svg'),
                            format='svg', dpi=1200
                        )
                        plt.close()

                mae_base_parts = []
                mae_agg_parts = []
                N = dev_target.shape[1]
                p = max(int(N/4), 1)
                for i in range(0, N, p):
                    mae_base_parts.append(
                        np.mean(
                            np.abs(dev_target[:, i:i+p] - pred_mu[:, i:i+p])
                        )
                    )
                    mae_agg_parts.append(
                        np.mean(
                            np.abs(dev_target[:, i:i+p] - pred_mu_agg[:, i:i+p])
                        )
                    )


                print('-------------------------------------------------------')
                print('{0}, {1}, {2}, mae_base:{3}, mae_agg:{4}'.format(
                    base_model_name, agg_method, level, mae_base, mae_agg)
                )
                print('{0}, {1}, {2}, crps_base:{3}, crps_agg:{4}'.format(
                    base_model_name, agg_method, level, crps_base, crps_agg)
                )
                print('mae_base_parts:', mae_base_parts)
                print('mae_agg_parts:', mae_agg_parts)
                print('-------------------------------------------------------')


writer.close()
            #import ipdb
            #ipdb.set_trace()
# ----- End: base models training ----- #

# ----- Start: Inference models ----- #
print('\n Starting Inference Models')

test_inputs_dict = dict()
test_targets_dict = dict()
test_targets_dict_leak = dict()
test_norm_dict = dict()
test_feats_in_dict = dict()
test_feats_tgt_dict = dict()
test_inputs_gaps_dict = dict()
test_targets_gaps_dict = dict()
N_input, N_output = 0, 0
for agg_method in args.aggregate_methods:
    test_inputs_dict[agg_method] = dict()
    test_targets_dict[agg_method] = dict()
    test_targets_dict_leak[agg_method] = dict()
    test_norm_dict[agg_method] = dict()
    test_feats_in_dict[agg_method] = dict()
    test_feats_tgt_dict[agg_method] = dict()
    test_inputs_gaps_dict[agg_method] = dict()
    test_targets_gaps_dict[agg_method] = dict()

    if agg_method in ['wavelet']:
        levels = list(range(1, args.wavelet_levels+3))
    else:
        levels = args.K_list

    for level in levels:
        dataset = data_processor.get_processed_data(args, agg_method, level)
        test_inputs, test_targets = [], []
        test_feats_in, test_feats_tgt = [], []
        test_norm = []
        test_inputs_gaps, test_targets_gaps = [], []
        for i, gen_test in enumerate(dataset['testloader']):
            (
                batch_test_inputs, batch_test_targets,
                batch_test_feats_in, batch_test_feats_tgt,
                batch_test_norm,
                _, _, _, batch_test_inputs_gaps, batch_test_targets_gaps
            ) = gen_test

            test_inputs.append(batch_test_inputs)
            test_targets.append(batch_test_targets)
            test_feats_in.append(batch_test_feats_in)
            test_feats_tgt.append(batch_test_feats_tgt)
            test_norm.append(batch_test_norm)
            test_inputs_gaps.append(batch_test_inputs_gaps)
            test_targets_gaps.append(batch_test_targets_gaps)

        test_inputs  = torch.cat(test_inputs, dim=0)#, dtype=torch.float32).to(args.device)
        test_targets = torch.cat(test_targets, dim=0)#, dtype=torch.float32).to(args.device)
        test_feats_in  = torch.cat(test_feats_in, dim=0)#, dtype=torch.float32).to(args.device)
        test_feats_tgt = torch.cat(test_feats_tgt, dim=0)#, dtype=torch.float32).to(args.device)
        test_norm = torch.cat(test_norm, dim=0)#, dtype=torch.float32).to(args.device)
        test_inputs_gaps  = torch.cat(test_inputs_gaps, dim=0)#, dtype=torch.float32).to(args.device)
        test_targets_gaps = torch.cat(test_targets_gaps, dim=0)#, dtype=torch.float32).to(args.device)

        test_inputs_dict[agg_method][level] = test_inputs
        test_targets_dict[agg_method][level] = test_targets
        test_targets_dict_leak[agg_method][level], _ = utils.normalize(
            test_targets, test_norm
        )
        test_norm_dict[agg_method][level] = test_norm
        test_feats_in_dict[agg_method][level] = test_feats_in
        test_feats_tgt_dict[agg_method][level] = test_feats_tgt
        test_inputs_gaps_dict[agg_method][level] = test_inputs_gaps
        test_targets_gaps_dict[agg_method][level] = test_targets_gaps

        if level == 1:
            N_input = dataset['N_input']
            N_output = dataset['N_output']

assert N_input > 0
assert N_output > 0
#criterion = torch.nn.MSELoss()

#import ipdb
#ipdb.set_trace()
if args.learnK:
    for inf_model_name in args.inference_model_names:

        if inf_model_name in ['MSE']:
            index_models_dict = index_models['seq2seqmse']['sum']
            inf_net = inf_index_models.MSE(index_models_dict)
            inf_test_inputs_dict = test_inputs_dict['sum']
            inf_test_targets_dict = test_targets_dict_leak['sum']
            inf_test_norm_dict = test_norm_dict['sum']
            inf_test_targets = test_targets_dict['sum'][1]
            inf_norm = test_norm_dict['sum'][1]
            inf_test_feats_in_dict = test_feats_in_dict['sum']
            inf_test_feats_tgt_dict = test_feats_tgt_dict['sum']
            inf_test_inputs_gaps_dict = test_inputs_gaps_dict['sum']

        elif inf_model_name in ['NLL']:
            index_models_dict = index_models['seq2seqnll']['sum']
            inf_net = inf_index_models.NLL(index_models_dict)
            inf_test_inputs_dict = test_inputs_dict['sum']
            inf_test_targets_dict = test_targets_dict_leak['sum']
            inf_test_norm_dict = test_norm_dict['sum']
            inf_test_targets = test_targets_dict['sum'][1]
            inf_norm = test_norm_dict['sum'][1]
            inf_test_feats_in_dict = test_feats_in_dict['sum']
            inf_test_feats_tgt_dict = test_feats_tgt_dict['sum']
            inf_test_inputs_gaps_dict = test_inputs_gaps_dict['sum']

        elif inf_model_name in ['seq2seqmse_optst']:
            index_models_dict = index_models['seq2seqmse']
            inf_net = inf_index_models.OPT_st(
                args.K_list, index_models_dict, args.device
            )
            inf_test_inputs_dict = test_inputs_dict
            inf_test_targets_dict = test_targets_dict_leak
            inf_test_norm_dict = test_norm_dict
            inf_test_targets = test_targets_dict['sum'][1]
            inf_norm = test_norm_dict['sum'][1]
            inf_test_feats_in_dict = test_feats_in_dict
            inf_test_feats_tgt_dict = test_feats_tgt_dict
            inf_test_inputs_gaps_dict = test_inputs_gaps_dict

        elif inf_model_name in ['seq2seqnll_optst']:
            index_models_dict = index_models['seq2seqnll']
            inf_net = inf_index_models.OPT_st(
                args.K_list, index_models_dict, args.device
            )
            inf_test_inputs_dict = test_inputs_dict
            inf_test_targets_dict = test_targets_dict_leak
            inf_test_norm_dict = test_norm_dict
            inf_test_targets = test_targets_dict['sum'][1]
            inf_norm = test_norm_dict['sum'][1]
            inf_test_feats_in_dict = test_feats_in_dict
            inf_test_feats_tgt_dict = test_feats_tgt_dict
            inf_test_inputs_gaps_dict = test_inputs_gaps_dict

        elif inf_model_name in ['seq2seqmse_opttrend']:
            base_models_dict = index_models['seq2seqmse']
            inf_net = inf_index_models.OPT_st(
                args.K_list, base_models_dict, args.device,
                disable_sum=True
            )
            inf_test_inputs_dict = test_inputs_dict
            inf_test_targets_dict = test_targets_dict_leak
            inf_test_norm_dict = test_norm_dict
            inf_test_targets = test_targets_dict['sum'][1]
            inf_norm = test_norm_dict['sum'][1]
            inf_test_feats_in_dict = test_feats_in_dict
            inf_test_feats_tgt_dict = test_feats_tgt_dict
            inf_test_inputs_gaps_dict = test_inputs_gaps_dict
    
        elif inf_model_name in ['seq2seqnll_opttrend']:
            base_models_dict = index_models['seq2seqnll']
            inf_net = inf_index_models.OPT_st(
                args.K_list, base_models_dict, args.device,
                disable_sum=True
            )
            inf_test_inputs_dict = test_inputs_dict
            inf_test_targets_dict = test_targets_dict_leak
            inf_test_norm_dict = test_norm_dict
            inf_test_targets = test_targets_dict['sum'][1]
            inf_norm = test_norm_dict['sum'][1]
            inf_test_feats_in_dict = test_feats_in_dict
            inf_test_feats_tgt_dict = test_feats_tgt_dict
            inf_test_inputs_gaps_dict = test_inputs_gaps_dict

        if not args.leak_agg_targets:
            inf_test_targets_dict = None

        inf_net.eval()
        (
            pred_mu, pred_std, metric_mse, metric_dtw, metric_tdi,
            metric_crps, metric_mae
        ) = eval_inf_index_model(
            args, inf_net, inf_test_inputs_dict, inf_test_norm_dict,
            inf_test_targets, inf_norm,
            inf_test_feats_in_dict, inf_test_feats_tgt_dict,
            inf_test_inputs_gaps_dict,
            args.gamma, N_input, N_output,
            inf_test_targets_dict=inf_test_targets_dict, verbose=1
        )
        inference_models[inf_model_name] = inf_net
        metric_mse = metric_mse.item()
    
        print('Metrics for Inference model {}: MAE:{:f}, CRPS:{:f}, MSE:{:f}, DTW:{:f}, TDI:{:f}'.format(
            inf_model_name, metric_mae, metric_crps, metric_mse, metric_dtw, metric_tdi)
        )
    
        model2metrics = utils.add_metrics_to_dict(
            model2metrics, inf_model_name,
            metric_mse, metric_dtw, metric_tdi, metric_crps, metric_mae
        )
        infmodel2preds[inf_model_name] = pred_mu
        output_dir = os.path.join(args.output_dir, args.dataset_name)
        os.makedirs(output_dir, exist_ok=True)
        utils.write_arr_to_file(
            output_dir, inf_model_name,
            utils.unnormalize(test_inputs_dict['sum'][1].detach().numpy(), inf_norm.detach().numpy()),
            test_targets_dict['sum'][1].detach().numpy(),
            pred_mu.detach().numpy(),
            pred_std.detach().numpy()
        )
else:
    for inf_model_name in args.inference_model_names:
    
        if inf_model_name in ['DILATE']:
            base_models_dict = base_models['seq2seqdilate']['sum']
            inf_net = inf_models.DILATE(base_models_dict)
            inf_test_inputs_dict = test_inputs_dict['sum']
            inf_test_targets_dict = test_targets_dict_leak['sum']
            inf_test_norm_dict = test_norm_dict['sum']
            inf_test_targets = test_targets_dict['sum'][1]
            inf_norm = test_norm_dict['sum'][1]
            inf_test_feats_in_dict = test_feats_in_dict['sum']
            inf_test_feats_tgt_dict = test_feats_tgt_dict['sum']
    
        elif inf_model_name in ['MSE']:
            base_models_dict = base_models['seq2seqmse']['sum']
            inf_net = inf_models.MSE(base_models_dict)
            inf_test_inputs_dict = test_inputs_dict['sum']
            inf_test_targets_dict = test_targets_dict_leak['sum']
            inf_test_norm_dict = test_norm_dict['sum']
            inf_test_targets = test_targets_dict['sum'][1]
            inf_norm = test_norm_dict['sum'][1]
            inf_test_feats_in_dict = test_feats_in_dict['sum']
            inf_test_feats_tgt_dict = test_feats_tgt_dict['sum']
    
        elif inf_model_name in ['NLL']:
            base_models_dict = base_models['seq2seqnll']['sum']
            inf_net = inf_models.NLL(base_models_dict)
            inf_test_inputs_dict = test_inputs_dict['sum']
            inf_test_targets_dict = test_targets_dict_leak['sum']
            inf_test_norm_dict = test_norm_dict['sum']
            inf_test_targets = test_targets_dict['sum'][1]
            inf_norm = test_norm_dict['sum'][1]
            inf_test_feats_in_dict = test_feats_in_dict['sum']
            inf_test_feats_tgt_dict = test_feats_tgt_dict['sum']
    
        elif inf_model_name in ['seq2seqmse_dualtpp']:
            base_models_dict = base_models['seq2seqmse']['sum']
            inf_net = inf_models.DualTPP(args.K_list, base_models_dict)
            inf_test_inputs_dict = test_inputs_dict['sum']
            inf_test_targets_dict = test_targets_dict_leak['sum']
            inf_test_norm_dict = test_norm_dict['sum']
            inf_test_targets = test_targets_dict['sum'][1]
            inf_norm = test_norm_dict['sum'][1]
            inf_test_feats_in_dict = test_feats_in_dict['sum']
            inf_test_feats_tgt_dict = test_feats_tgt_dict['sum']
    
        elif inf_model_name in ['seq2seqnll_dualtpp']:
            base_models_dict = base_models['seq2seqnll']['sum']
            inf_net = inf_models.DualTPP(args.K_list, base_models_dict)
            inf_test_inputs_dict = test_inputs_dict['sum']
            inf_test_targets_dict = test_targets_dict_leak['sum']
            inf_test_norm_dict = test_norm_dict['sum']
            inf_test_targets = test_targets_dict['sum'][1]
            inf_norm = test_norm_dict['sum'][1]
            inf_test_feats_in_dict = test_feats_in_dict['sum']
            inf_test_feats_tgt_dict = test_feats_tgt_dict['sum']
    
        elif inf_model_name in ['seq2seqmse_optls']:
            base_models_dict = base_models['seq2seqmse']['leastsquare']
            inf_net = inf_models.OPT_ls(args.K_list, base_models_dict)
            inf_test_inputs_dict = test_inputs_dict['leastsquare']
            inf_test_targets_dict = test_targets_dict_leak['leastsquare']
            inf_test_norm_dict = test_norm_dict['leastsquare']
            inf_test_targets = test_targets_dict['sum'][1]
            inf_norm = test_norm_dict['sum'][1]
            inf_test_feats_in_dict = test_feats_in_dict['leastsquare']
            inf_test_feats_tgt_dict = test_feats_tgt_dict['leastsquare']
    
        elif inf_model_name in ['seq2seqnll_optls']:
            base_models_dict = base_models['seq2seqnll']['leastsquare']
            inf_net = inf_models.OPT_ls(args.K_list, base_models_dict)
            inf_test_inputs_dict = test_inputs_dict['leastsquare']
            inf_test_targets_dict = test_targets_dict_leak['leastsquare']
            inf_test_norm_dict = test_norm_dict['leastsquare']
            inf_test_targets = test_targets_dict['sum'][1]
            inf_norm = test_norm_dict['sum'][1]
            inf_test_feats_in_dict = test_feats_in_dict['leastsquare']
            inf_test_feats_tgt_dict = test_feats_tgt_dict['leastsquare']
    
        elif inf_model_name in ['seq2seqmse_optst']:
            base_models_dict = base_models['seq2seqmse']
            inf_net = inf_models.OPT_st(args.K_list, base_models_dict)
            inf_test_inputs_dict = test_inputs_dict
            inf_test_targets_dict = test_targets_dict_leak
            inf_test_norm_dict = test_norm_dict
            inf_test_targets = test_targets_dict['sum'][1]
            inf_norm = test_norm_dict['sum'][1]
            inf_test_feats_in_dict = test_feats_in_dict
            inf_test_feats_tgt_dict = test_feats_tgt_dict
    
        elif inf_model_name in ['seq2seqnll_optst']:
            base_models_dict = base_models['seq2seqnll']
            inf_net = inf_models.OPT_st(args.K_list, base_models_dict)
            inf_test_inputs_dict = test_inputs_dict
            inf_test_targets_dict = test_targets_dict_leak
            inf_test_norm_dict = test_norm_dict
            inf_test_targets = test_targets_dict['sum'][1]
            inf_norm = test_norm_dict['sum'][1]
            inf_test_feats_in_dict = test_feats_in_dict
            inf_test_feats_tgt_dict = test_feats_tgt_dict
    
        elif inf_model_name in ['seq2seqmse_opttrend']:
            base_models_dict = base_models['seq2seqmse']
            inf_net = inf_models.OPT_st(
                args.K_list, base_models_dict, disable_sum=True
            )
            inf_test_inputs_dict = test_inputs_dict
            inf_test_targets_dict = test_targets_dict_leak
            inf_test_norm_dict = test_norm_dict
            inf_test_targets = test_targets_dict['sum'][1]
            inf_norm = test_norm_dict['sum'][1]
            inf_test_feats_in_dict = test_feats_in_dict
            inf_test_feats_tgt_dict = test_feats_tgt_dict
    
        elif inf_model_name in ['seq2seqnll_opttrend']:
            base_models_dict = base_models['seq2seqnll']
            inf_net = inf_models.OPT_st(
                args.K_list, base_models_dict, disable_sum=True
            )
            inf_test_inputs_dict = test_inputs_dict
            inf_test_targets_dict = test_targets_dict_leak
            inf_test_norm_dict = test_norm_dict
            inf_test_targets = test_targets_dict['sum'][1]
            inf_norm = test_norm_dict['sum'][1]
            inf_test_feats_in_dict = test_feats_in_dict
            inf_test_feats_tgt_dict = test_feats_tgt_dict
    
        elif inf_model_name in ['seq2seqnll_optklst']:
            base_models_dict = base_models['seq2seqnll']
            inf_net = inf_models.OPT_KL_st(
                args.K_list, base_models_dict,
                agg_methods=['sum', 'slope'],
            )
            inf_test_inputs_dict = test_inputs_dict
            inf_test_targets_dict = test_targets_dict_leak
            inf_test_norm_dict = test_norm_dict
            inf_test_targets = test_targets_dict['sum'][1]
            inf_norm = test_norm_dict['sum'][1]
            inf_test_feats_in_dict = test_feats_in_dict
            inf_test_feats_tgt_dict = test_feats_tgt_dict
    
        elif inf_model_name in ['seq2seqnll_optkls']:
            base_models_dict = base_models['seq2seqnll']
            inf_net = inf_models.OPT_KL_st(
                args.K_list, base_models_dict,
                agg_methods=['sum'],
            )
            inf_test_inputs_dict = test_inputs_dict
            inf_test_targets_dict = test_targets_dict_leak
            inf_test_norm_dict = test_norm_dict
            inf_test_targets = test_targets_dict['sum'][1]
            inf_norm = test_norm_dict['sum'][1]
            inf_test_feats_in_dict = test_feats_in_dict
            inf_test_feats_tgt_dict = test_feats_tgt_dict
    
        elif inf_model_name in ['seq2seqnll_optklt']:
            base_models_dict = base_models['seq2seqnll']
            inf_net = inf_models.OPT_KL_st(
                args.K_list, base_models_dict,
                agg_methods=['slope'],
            )
            inf_test_inputs_dict = test_inputs_dict
            inf_test_targets_dict = test_targets_dict_leak
            inf_test_norm_dict = test_norm_dict
            inf_test_targets = test_targets_dict['sum'][1]
            inf_norm = test_norm_dict['sum'][1]
            inf_test_feats_in_dict = test_feats_in_dict
            inf_test_feats_tgt_dict = test_feats_tgt_dict
    
        elif inf_model_name in ['seq2seqmse_wavelet']:
            base_models_dict = base_models['seq2seqmse']
            inf_net = inf_models.WAVELET(args.wavelet_levels, base_models_dict)
            inf_test_inputs_dict = test_inputs_dict
            inf_test_targets_dict = test_targets_dict_leak
            inf_test_norm_dict = test_norm_dict
            inf_test_targets = test_targets_dict['sum'][1]
            inf_norm = test_norm_dict['sum'][1]
    
        elif inf_model_name in ['seq2seqnll_wavelet']:
            base_models_dict = base_models['seq2seqnll']
            inf_net = inf_models.WAVELET(args.wavelet_levels, base_models_dict)
            inf_test_inputs_dict = test_inputs_dict
            inf_test_targets_dict = test_targets_dict_leak
            inf_test_norm_dict = test_norm_dict
            inf_test_targets = test_targets_dict['sum'][1]
            inf_norm = test_norm_dict['sum'][1]
    
        if not args.leak_agg_targets:
            inf_test_targets_dict = None
    
        inf_net.eval()
        pred_mu, pred_std, metric_mse, metric_dtw, metric_tdi, metric_crps, metric_mae = eval_inf_model(
            args, inf_net, inf_test_inputs_dict, inf_test_norm_dict,
            inf_test_targets, inf_norm,
            inf_test_feats_in_dict, inf_test_feats_tgt_dict,
            args.gamma, inf_test_targets_dict=inf_test_targets_dict, verbose=1
        )
        inference_models[inf_model_name] = inf_net
        metric_mse = metric_mse.item()
    
        print('Metrics for Inference model {}: MAE:{:f}, CRPS:{:f}, MSE:{:f}, DTW:{:f}, TDI:{:f}'.format(
            inf_model_name, metric_mae, metric_crps, metric_mse, metric_dtw, metric_tdi)
        )
    
        model2metrics = utils.add_metrics_to_dict(
            model2metrics, inf_model_name,
            metric_mse, metric_dtw, metric_tdi, metric_crps, metric_mae
        )
        infmodel2preds[inf_model_name] = pred_mu
        output_dir = os.path.join(args.output_dir, args.dataset_name)
        os.makedirs(output_dir, exist_ok=True)
        utils.write_arr_to_file(
            output_dir, inf_model_name,
            utils.unnormalize(test_inputs_dict['sum'][1].detach().numpy(), inf_norm.detach().numpy()),
            test_targets_dict['sum'][1].detach().numpy(),
            pred_mu.detach().numpy(),
            pred_std.detach().numpy()
        )


# ----- End: Inference models ----- #

with open(os.path.join(args.output_dir, 'results_'+args.dataset_name+'.txt'), 'w') as fp:

    fp.write('\nModel Name, MAE, DTW, TDI')
    for model_name, metrics_dict in model2metrics.items():
        fp.write(
            '\n{}, {:.6f}, {:.6f}, {:.6f}, {:.6f}, {:.6f}'.format(
                model_name,
                metrics_dict['mae'],
                metrics_dict['crps'],
                metrics_dict['mse'],
                metrics_dict['dtw'],
                metrics_dict['tdi'],
            )
        )

for model_name, metrics_dict in model2metrics.items():
    for metric, metric_val in metrics_dict.items():
        model2metrics[model_name][metric] = str(metric_val)
with open(os.path.join(args.output_dir, 'results_'+args.dataset_name+'.json'), 'w') as fp:
    json.dump(model2metrics, fp)

# Visualize results


if args.plot_anecdotes:
    for ind in range(1,51):
        plt.figure()
        plt.rcParams['figure.figsize'] = (16.0,8.0)
        k = 1
        for inf_mdl_name, pred_mu in infmodel2preds.items():

            input = test_inputs_dict['sum'][1].detach().cpu().numpy()[ind,:,:]
            target = test_targets_dict['sum'][1].detach().cpu().numpy()[ind,:,:]
            pred_mu = pred_mu.detach().cpu().numpy()[ind,:,:]

            plt.subplot(len(inference_models),1,k)
            plt.plot(range(0,args.N_input) ,input,label='input',linewidth=3)
            plt.plot(range(args.N_input-1,args.N_input+args.N_output), np.concatenate([ input[args.N_input-1:args.N_input], target ]) ,label='target',linewidth=3)
            plt.plot(range(args.N_input-1,args.N_input+args.N_output),  np.concatenate([ input[args.N_input-1:args.N_input], pred_mu ])  ,label=inf_mdl_name,linewidth=3)
            plt.xticks(range(0,40,2))
            plt.legend()
            k = k+1

        plt.show()
