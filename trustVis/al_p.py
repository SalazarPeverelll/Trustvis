########################################################################################################################
#                                                          IMPORT                                                      #
########################################################################################################################
import torch
import sys
sys.path.append('..')
import os
import json
import time
import numpy as np
import argparse

from torch.utils.data import DataLoader
from torch.utils.data import WeightedRandomSampler
from umap.umap_ import find_ab_params

from singleVis.custom_weighted_random_sampler import CustomWeightedRandomSampler
from singleVis.SingleVisualizationModel import VisModel
from singleVis.losses import UmapLoss, ReconstructionLoss, TemporalLoss, DVILoss, DummyTemporalLoss,ActiveLearningLoss, DVIALLoss
from singleVis.edge_dataset import DVIDataHandler
from singleVis.trainer import PROXYALMODITrainer
from singleVis.data import NormalDataProvider
from singleVis.spatial_skeleton_edge_constructor import OriginSingleEpochSpatialEdgeConstructor
from singleVis.projector import DVIProjector
from singleVis.eval.evaluator import Evaluator
from singleVis.utils import find_neighbor_preserving_rate
from singleVis.visualizer import visualizer
import numpy as np
# from trustVis.skeleton_generator import CenterSkeletonGenerator
########################################################################################################################
#                                                    PARAMETERS                                                   #
########################################################################################################################
"""This serve as an example of DeepVisualInsight implementation in pytorch."""
VIS_METHOD = "DVI" # DeepVisualInsight

########################################################################################################################
#                                                     LOAD PARAMETERS                                                  #
########################################################################################################################
parser = argparse.ArgumentParser(description='Process hyperparameters...')

# get workspace dir
current_path = os.getcwd()

parent_path = os.path.dirname(current_path)

new_path = os.path.join(parent_path, 'training_dynamic')


parser.add_argument('--content_path', type=str,default=new_path)
parser.add_argument('--base', type=str,default='proxy')
parser.add_argument('--start', type=int,default=1)
parser.add_argument('--end', type=int,default=3)
parser.add_argument('--epoch', type=int,default=3)
args = parser.parse_args()



CONTENT_PATH = args.content_path
sys.path.append(CONTENT_PATH)
with open(os.path.join(CONTENT_PATH, "config.json"), "r") as f:
    config = json.load(f)
config = config[VIS_METHOD]


CLASSES = config["CLASSES"]
PREPROCESS = config["VISUALIZATION"]["PREPROCESS"]
GPU_ID = config["GPU"]
EPOCH_START = args.epoch
EPOCH_END = args.epoch
EPOCH_PERIOD = 1

# Training parameter (subject model)
TRAINING_PARAMETER = config["TRAINING"]
NET = TRAINING_PARAMETER["NET"]
LEN = TRAINING_PARAMETER["train_num"]

# Training parameter (visualization model)
VISUALIZATION_PARAMETER = config["VISUALIZATION"]
LAMBDA1 = VISUALIZATION_PARAMETER["LAMBDA1"]
LAMBDA1 = 1
LAMBDA2 = VISUALIZATION_PARAMETER["LAMBDA2"]
# B_N_EPOCHS = VISUALIZATION_PARAMETER["BOUNDARY"]["B_N_EPOCHS"]
B_N_EPOCHS = 0
L_BOUND = VISUALIZATION_PARAMETER["BOUNDARY"]["L_BOUND"]
ENCODER_DIMS = VISUALIZATION_PARAMETER["ENCODER_DIMS"]
DECODER_DIMS = VISUALIZATION_PARAMETER["DECODER_DIMS"]
S_N_EPOCHS = VISUALIZATION_PARAMETER["S_N_EPOCHS"]
N_NEIGHBORS = VISUALIZATION_PARAMETER["N_NEIGHBORS"]
PATIENT = VISUALIZATION_PARAMETER["PATIENT"]
# MAX_EPOCH = VISUALIZATION_PARAMETER["MAX_EPOCH"]
MAX_EPOCH = 8

VIS_MODEL_NAME = 'trustvis'
SAVED_NAME = VIS_MODEL_NAME
EVALUATION_NAME = VISUALIZATION_PARAMETER["EVALUATION_NAME"]

# Define hyperparameters
GPU_ID = 1
DEVICE = torch.device("cuda:{}".format(GPU_ID) if torch.cuda.is_available() else "cpu")

import Model.model as subject_model
net = eval("subject_model.{}()".format(NET))

########################################################################################################################
#                                                    TRAINING SETTING                                                  #
########################################################################################################################
BASE_MODEL_NAME = args.base
# Define data_provider
data_provider = NormalDataProvider(CONTENT_PATH, net, EPOCH_START, EPOCH_END, EPOCH_PERIOD, device=DEVICE, classes=CLASSES, epoch_name='Epoch', verbose=1)
# Define visualization models
model = VisModel(ENCODER_DIMS, DECODER_DIMS)

# Define Losses
negative_sample_rate = 5
min_dist = .1
_a, _b = find_ab_params(1.0, min_dist)
umap_loss_fn = UmapLoss(negative_sample_rate, DEVICE, _a, _b, repulsion_strength=1.0)
recon_loss_fn = ReconstructionLoss(beta=1.0)
# Define Projector
projector = DVIProjector(vis_model=model, content_path=CONTENT_PATH, vis_model_name=BASE_MODEL_NAME, device=DEVICE) # vis_model_name init dvi

start_flag = 1
prev_model = VisModel(ENCODER_DIMS, DECODER_DIMS)

for iteration in range(EPOCH_START, EPOCH_END+EPOCH_PERIOD, EPOCH_PERIOD):
    # Define DVI Loss
    if start_flag:
        temporal_loss_fn = DummyTemporalLoss(DEVICE)
        criterion = DVIALLoss(umap_loss_fn, recon_loss_fn, temporal_loss_fn, lambd1=LAMBDA1, lambd2=0.0, device=DEVICE)
        start_flag = 0
    else:
        # TODO AL mode, redefine train_representation
        prev_data = data_provider.train_representation(iteration-EPOCH_PERIOD)
        curr_data = data_provider.train_representation(iteration)
        npr = find_neighbor_preserving_rate(prev_data, curr_data, N_NEIGHBORS)
        temporal_loss_fn = TemporalLoss(w_prev, DEVICE)
        criterion = DVILoss(umap_loss_fn, recon_loss_fn, temporal_loss_fn, lambd1=LAMBDA1, lambd2=torch.from_numpy(LAMBDA2*npr), device=DEVICE)
    
       
    # Define training parameters
    optimizer = torch.optim.Adam(model.parameters(), lr=.01, weight_decay=1e-5)
    lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=4, gamma=.1)
    # Define Edge dataset
    t0 = time.time()
    # spatial_cons = OriginSingleEpochSpatialEdgeConstructor(data_provider, iteration, S_N_EPOCHS, B_N_EPOCHS, N_NEIGHBORS)
    # edge_to, edge_from, probs, feature_vectors, attention = spatial_cons.construct()
    t1 = time.time()

    # probs = probs / (probs.max()+1e-3)
    # eliminate_zeros = probs> 1e-3    #1e-3
    # edge_to = edge_to[eliminate_zeros]
    # edge_from = edge_from[eliminate_zeros]
    # probs = probs[eliminate_zeros]
    
    # dataset = DVIDataHandler(edge_to, edge_from, feature_vectors, attention)

    # n_samples = int(np.sum(S_N_EPOCHS * probs) // 1)
    # # chose sampler based on the number of dataset
    # if len(edge_to) > pow(2,24):
    #     sampler = CustomWeightedRandomSampler(probs, n_samples, replacement=True)
    # else:
    #     sampler = WeightedRandomSampler(probs, n_samples, replacement=True)
    # edge_loader = DataLoader(dataset, batch_size=2000, sampler=sampler, num_workers=8, prefetch_factor=10)

    ########################################################################################################################
    #                                                       TRAIN                                                          #
    ########################################################################################################################
    file_path = os.path.join(data_provider.content_path, "Model", "Epoch_{}".format(iteration), "{}.pth".format(BASE_MODEL_NAME))
    save_model = torch.load(file_path, map_location="cpu")
    model.load_state_dict(save_model["state_dict"])

    trainer = PROXYALMODITrainer(model, criterion, optimizer, lr_scheduler, edge_loader=None, DEVICE=DEVICE,iteration=iteration, data_provider=data_provider, prev_model=prev_model, S_N_EPOCHS=S_N_EPOCHS, B_N_EPOCHS=B_N_EPOCHS, N_NEIGHBORS=N_NEIGHBORS,threshold=5,resolution=800)

    t2=time.time()
    trainer.train(PATIENT, MAX_EPOCH)
    t3 = time.time()

    # save result
    save_dir = data_provider.model_path
    trainer.record_time(save_dir, "time_{}".format(VIS_MODEL_NAME), "complex_construction", str(iteration), t1-t0)
    trainer.record_time(save_dir, "time_{}".format(VIS_MODEL_NAME), "training", str(iteration), t3-t2)
    save_dir = os.path.join(data_provider.model_path, "Epoch_{}".format(iteration))
    trainer.save(save_dir=save_dir, file_name="{}".format(SAVED_NAME))

    print("Finish epoch {}...".format(iteration))

    prev_model.load_state_dict(model.state_dict())
    for param in prev_model.parameters():
        param.requires_grad = False
    w_prev = dict(prev_model.named_parameters())

print('al runtime', t3-t0)
########################################################################################################################
#                                                      VISUALIZATION                                                   #
########################################################################################################################
projector = DVIProjector(vis_model=model, content_path=CONTENT_PATH, vis_model_name=VIS_MODEL_NAME, device=DEVICE) # vis_model_name init dvi

vis = visualizer(data_provider, projector, 200, "tab10")
save_dir = os.path.join(data_provider.content_path, VIS_MODEL_NAME)
if not os.path.exists(save_dir):
    os.mkdir(save_dir)
for i in range(EPOCH_START, EPOCH_END+1, EPOCH_PERIOD):
    vis.savefig(i, path=os.path.join(save_dir, "{}_{}_{}.png".format(VIS_MODEL_NAME, i, VIS_METHOD)))

    
########################################################################################################################
#                                                       EVALUATION                                                     #
########################################################################################################################

evaluator = Evaluator(data_provider, projector)

Evaluation_NAME = 'trustvis_al_eval'
for i in range(EPOCH_START, EPOCH_END+1, EPOCH_PERIOD):
    evaluator.save_epoch_eval(i, 15, temporal_k=5, file_name="{}".format(Evaluation_NAME))