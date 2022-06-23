import torch
import sys
import os
import json
import time
import numpy as np
import argparse

from torch.utils.data import DataLoader
from torch.utils.data import WeightedRandomSampler
from umap.umap_ import find_ab_params

from singleVis.custom_weighted_random_sampler import CustomWeightedRandomSampler
from singleVis.SingleVisualizationModel import SingleVisualizationModel
from singleVis.losses import HybridLoss, SmoothnessLoss, UmapLoss, ReconstructionLoss
from singleVis.edge_dataset import HybridDataHandler
from singleVis.trainer import HybridVisTrainer
from singleVis.data import NormalDataProvider
import singleVis.config as config
from singleVis.eval.evaluator import Evaluator
from singleVis.spatial_edge_constructor import kcHybridSpatialEdgeConstructor
from singleVis.temporal_edge_constructor import GlobalTemporalEdgeConstructor

########################################################################################################################
#                                                     LOAD PARAMETERS                                                  #
########################################################################################################################


parser = argparse.ArgumentParser(description='Process hyperparameters...')
parser.add_argument('--content_path', type=str)
args = parser.parse_args()

CONTENT_PATH = args.content_path
sys.path.append(CONTENT_PATH)
from config import config

SETTING = config["SETTING"]
CLASSES = config["CLASSES"]
DATASET = config["DATASET"]
PREPROCESS = config["VISUALIZATION"]["PREPROCESS"]
GPU_ID = config["GPU"]
EPOCH_START = config["EPOCH_START"]
EPOCH_END = config["EPOCH_END"]
EPOCH_PERIOD = config["EPOCH_PERIOD"]

# Training parameter (subject model)
TRAINING_PARAMETER = config["TRAINING"]
NET = TRAINING_PARAMETER["NET"]
LEN = TRAINING_PARAMETER["train_num"]

# Training parameter (visualization model)
VISUALIZATION_PARAMETER = config["VISUALIZATION"]
LAMBDA = VISUALIZATION_PARAMETER["LAMBDA"]
S_LAMBDA = VISUALIZATION_PARAMETER["S_LAMBDA"]
B_N_EPOCHS = VISUALIZATION_PARAMETER["BOUNDARY"]["B_N_EPOCHS"]
L_BOUND = VISUALIZATION_PARAMETER["BOUNDARY"]["L_BOUND"]
INIT_NUM = VISUALIZATION_PARAMETER["INIT_NUM"]
ALPHA = VISUALIZATION_PARAMETER["ALPHA"]
BETA = VISUALIZATION_PARAMETER["BETA"]
MAX_HAUSDORFF = VISUALIZATION_PARAMETER["MAX_HAUSDORFF"]
HIDDEN_LAYER = VISUALIZATION_PARAMETER["HIDDEN_LAYER"]
S_N_EPOCHS = VISUALIZATION_PARAMETER["S_N_EPOCHS"]
T_N_EPOCHS = VISUALIZATION_PARAMETER["T_N_EPOCHS"]
N_NEIGHBORS = VISUALIZATION_PARAMETER["N_NEIGHBORS"]
PATIENT = VISUALIZATION_PARAMETER["PATIENT"]
MAX_EPOCH = VISUALIZATION_PARAMETER["MAX_EPOCH"]
SEGMENTS = VISUALIZATION_PARAMETER["SEGMENTS"]
RESUME_SEG = VISUALIZATION_PARAMETER["RESUME_SEG"]

# define hyperparameters
DEVICE = torch.device("cuda:{}".format(GPU_ID) if torch.cuda.is_available() else "cpu")

import Model.model as subject_model
net = eval("subject_model.{}()".format(NET))

########################################################################################################################
#                                                    TRAINING SETTING                                                  #
########################################################################################################################
data_provider = NormalDataProvider(CONTENT_PATH, net, EPOCH_START, EPOCH_END, EPOCH_PERIOD, split=-1, device=DEVICE, classes=CLASSES,verbose=1)
if PREPROCESS:
    data_provider.initialize(LEN//10, l_bound=L_BOUND)

model = SingleVisualizationModel(input_dims=512, output_dims=2, units=256, hidden_layer=HIDDEN_LAYER)
negative_sample_rate = 5
min_dist = .1
_a, _b = find_ab_params(1.0, min_dist)
umap_loss_fn = UmapLoss(negative_sample_rate, DEVICE, _a, _b, repulsion_strength=1.0)
recon_loss_fn = ReconstructionLoss(beta=1.0)
smooth_loss_fn = SmoothnessLoss(margin=0.25)
criterion = HybridLoss(umap_loss_fn, recon_loss_fn, smooth_loss_fn, lambd1=LAMBDA, lambd2=S_LAMBDA)

# Resume from a check point
if RESUME_SEG in range(len(SEGMENTS)):
    prev_epoch = SEGMENTS[RESUME_SEG][0]
    with open(os.path.join(data_provider.content_path, "selected_idxs", "selected_{}.json".format(prev_epoch)), "r") as f:
        prev_selected = json.load(f)
    save_model_path = os.path.join(data_provider.model_path, "tnn_hybrid_{}.pth".format(RESUME_SEG))
    save_model = torch.load(save_model_path, map_location=torch.device("cpu"))
    model.load_state_dict(save_model["state_dict"])
    prev_data = torch.from_numpy(data_provider.train_representation(prev_epoch)[prev_selected]).to(dtype=torch.float32)
    start_point = RESUME_SEG - 1
    prev_embedding = model.encoder(prev_data).detach().numpy()
    print("Resume from {}-th segment with {} points...".format(RESUME_SEG, INIT_NUM))
else: 
    prev_selected = np.random.choice(np.arange(LEN), size=INIT_NUM, replace=False)
    prev_embedding = None
    start_point = len(SEGMENTS)-1

for seg in range(start_point,-1,-1):
    epoch_start, epoch_end = SEGMENTS[seg]
    data_provider.update_interval(epoch_s=epoch_start, epoch_e=epoch_end)

    optimizer = torch.optim.Adam(model.parameters(), lr=.01, weight_decay=1e-5)
    lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=4, gamma=.1)

    t0 = time.time()
    spatial_cons = kcHybridSpatialEdgeConstructor(data_provider=data_provider, init_num=INIT_NUM, s_n_epochs=S_N_EPOCHS, b_n_epochs=B_N_EPOCHS, n_neighbors=N_NEIGHBORS, MAX_HAUSDORFF=MAX_HAUSDORFF, ALPHA=ALPHA, BETA=BETA, init_idxs=prev_selected, init_embeddings=prev_embedding)
    s_edge_to, s_edge_from, s_probs, feature_vectors, embedded, coefficient, time_step_nums, time_step_idxs_list, knn_indices, sigmas, rhos, attention = spatial_cons.construct()
    
    # update prev_idxs and prev_embedding
    prev_selected = time_step_idxs_list[0]
    prev_data = torch.from_numpy(feature_vectors[:len(prev_selected)]).to(dtype=torch.float32, device=DEVICE)
    model.to(device=DEVICE)
    prev_embedding = model.encoder(prev_data).cpu().detach().numpy()

    temporal_cons = GlobalTemporalEdgeConstructor(X=feature_vectors, time_step_nums=time_step_nums, sigmas=sigmas, rhos=rhos, n_neighbors=N_NEIGHBORS, n_epochs=T_N_EPOCHS)
    t_edge_to, t_edge_from, t_probs = temporal_cons.construct()
    t1 = time.time()

    edge_to = np.concatenate((s_edge_to, t_edge_to),axis=0)
    edge_from = np.concatenate((s_edge_from, t_edge_from), axis=0)
    probs = np.concatenate((s_probs, t_probs), axis=0)
    probs = probs / (probs.max()+1e-3)
    eliminate_zeros = probs>1e-3
    edge_to = edge_to[eliminate_zeros]
    edge_from = edge_from[eliminate_zeros]
    probs = probs[eliminate_zeros]

    # save result
    save_dir = os.path.join(data_provider.model_path, "SV_time_tnn_hybrid.json")
    if not os.path.exists(save_dir):
        evaluation = dict()
    else:
        f = open(save_dir, "r")
        evaluation = json.load(f)
        f.close()
    evaluation["complex_construction"] = dict()
    evaluation["complex_construction"][str(seg)] = round(t1-t0, 3)
    with open(save_dir, 'w') as f:
        json.dump(evaluation, f)
    print("constructing timeVis complex for {}-th segment in {:.1f} seconds.".format(seg, t1-t0))


    dataset = HybridDataHandler(edge_to, edge_from, feature_vectors, attention, embedded, coefficient)
    n_samples = int(np.sum(S_N_EPOCHS * probs) // 1)
    # chosse sampler based on the number of dataset
    if len(edge_to) > 2^24:
        sampler = CustomWeightedRandomSampler(probs, n_samples, replacement=True)
    else:
        sampler = WeightedRandomSampler(probs, n_samples, replacement=True)
    edge_loader = DataLoader(dataset, batch_size=1000, sampler=sampler)

    ########################################################################################################################
    #                                                       TRAIN                                                          #
    ########################################################################################################################

    trainer = HybridVisTrainer(model, criterion, optimizer, lr_scheduler,edge_loader=edge_loader, DEVICE=DEVICE)

    t2=time.time()
    trainer.train(PATIENT, MAX_EPOCH)
    t3 = time.time()
    # save result
    save_dir = os.path.join(data_provider.model_path, "SV_time_tnn_hybrid.json")
    if not os.path.exists(save_dir):
        evaluation = dict()
    else:
        f = open(save_dir, "r")
        evaluation = json.load(f)
        f.close()

    evaluation["training"] = dict()
    evaluation["training"][str(seg)] = round(t3-t2, 3)
    with open(save_dir, 'w') as f:
        json.dump(evaluation, f)
    trainer.save(save_dir=data_provider.model_path, file_name="tnn_hybrid_{}".format(seg))
    
    model = trainer.model

# data_provider.update_interval(EPOCH_START, EPOCH_END)
# ########################################################################################################################
# #                                                      VISUALIZATION                                                   #
# ########################################################################################################################

# from singleVis.visualizer import visualizer

# vis = visualizer(data_provider, trainer.model, 200, 10, CLASSES)
# save_dir = os.path.join(data_provider.content_path, "img")
# if not os.path.exists(save_dir):
#     os.mkdir(save_dir)
# for i in range(EPOCH_START, EPOCH_END+1, 2*EPOCH_PERIOD):
#     vis.savefig(i, path=os.path.join(save_dir, "{}_{}_hybrid_tnn.png".format(DATASET, i)))

    
# ########################################################################################################################
# #                                                       EVALUATION                                                     #
# ########################################################################################################################
# EVAL_EPOCH_DICT = {
#     "mnist_full":[4, 12, 20],
#     "fmnist_full":[10, 30, 50],
#     "cifar10_full":[40, 120, 200],
#     "cifar10":[5,15,25,35,50,100,150,200]
# }
# eval_epochs = EVAL_EPOCH_DICT[DATASET]

# evaluator = Evaluator(data_provider, trainer)
# # evaluator.save_epoch_eval(eval_epochs[0], 10, temporal_k=3, save_corrs=True, file_name="test_evaluation_tnn")
# evaluator.save_epoch_eval(eval_epochs[0], 15, temporal_k=5, save_corrs=False, file_name="test_evaluation_tnn")
# # evaluator.save_epoch_eval(eval_epochs[0], 20, temporal_k=7, save_corrs=False, file_name="test_evaluation_tnn")

# # evaluator.save_epoch_eval(eval_epochs[1], 10, temporal_k=3, save_corrs=True, file_name="test_evaluation_tnn")
# evaluator.save_epoch_eval(eval_epochs[1], 15, temporal_k=5, save_corrs=False, file_name="test_evaluation_tnn")
# # evaluator.save_epoch_eval(eval_epochs[1], 20, temporal_k=7, save_corrs=False, file_name="test_evaluation_tnn")

# # evaluator.save_epoch_eval(eval_epochs[2], 10, temporal_k=3, save_corrs=True, file_name="test_evaluation_tnn")
# evaluator.save_epoch_eval(eval_epochs[2], 15, temporal_k=5, save_corrs=False, file_name="test_evaluation_tnn")
# # evaluator.save_epoch_eval(eval_epochs[2], 20, temporal_k=7, save_corrs=False, file_name="test_evaluation_tnn")