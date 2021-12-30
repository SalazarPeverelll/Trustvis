import torch
import sys
import os
import numpy as np
import time
import argparse

from torch.utils.data import DataLoader
from torch.utils.data import WeightedRandomSampler
from umap.umap_ import find_ab_params

from singleVis.custom_weighted_random_sampler import CustomWeightedRandomSampler
from singleVis.SingleVisualizationModel import SingleVisualizationModel
from singleVis.losses import SingleVisLoss, UmapLoss, ReconstructionLoss
from singleVis.edge_dataset import DataHandler
from singleVis.trainer import SingleVisTrainer
from singleVis.data import DataProvider
from singleVis.backend import construct_spatial_temporal_complex
import singleVis.config as config


parser = argparse.ArgumentParser(description='Process hyperparameters...')
parser.add_argument('--content_path', type=str)
parser.add_argument('-d','--dataset', choices=['online','cifar10', 'mnist', 'fmnist'])

args = parser.parse_args()

CONTENT_PATH = args.content_path
DATASET = args.dataset
LEN = config.dataset_config[DATASET]["TRAINING_LEN"]
LAMBDA = config.dataset_config[DATASET]["LAMBDA"]
DOWNSAMPLING_RATE = config.dataset_config[DATASET]["DOWNSAMPLING_RATE"]

# define hyperparameters

DEVICE = torch.device("cuda:1" if torch.cuda.is_available() else "cpu")
EPOCH_NUMS = config.dataset_config[DATASET]["training_config"]["EPOCH_NUM"]
TIME_STEPS = config.dataset_config[DATASET]["training_config"]["TIME_STEPS"]
TEMPORAL_PERSISTENT = config.dataset_config[DATASET]["training_config"]["TEMPORAL_PERSISTENT"]
NUMS = config.dataset_config[DATASET]["training_config"]["NUMS"]    # how many epoch should we go through for one pass
PATIENT = config.dataset_config[DATASET]["training_config"]["PATIENT"]
TEMPORAL_EDGE_WEIGHT = config.dataset_config[DATASET]["training_config"]["TEMPORAL_EDGE_WEIGHT"]

content_path = CONTENT_PATH
sys.path.append(content_path)

from Model.model import *
net = resnet18()
classes = ("airplane", "car", "bird", "cat", "deer", "dog", "frog", "horse", "ship", "truck")
selected_idxs = np.random.choice(np.arange(LEN), size=LEN*DOWNSAMPLING_RATE//1, replace=False)

data_provider = DataProvider(content_path, net, 1, TIME_STEPS, 1, split=-1, device=DEVICE, verbose=1)
# data_provider.initialize(LEN//10, l_bound=0.6)

time_start = time.time()
model = SingleVisualizationModel(input_dims=512, output_dims=2, units=256)
negative_sample_rate = 5
min_dist = .1
_a, _b = find_ab_params(1.0, min_dist)
umap_loss_fn = UmapLoss(negative_sample_rate, DEVICE, _a, _b, repulsion_strength=1.0)
recon_loss_fn = ReconstructionLoss(beta=1.0)
criterion = SingleVisLoss(umap_loss_fn, recon_loss_fn, lambd=LAMBDA)

optimizer = torch.optim.Adam(model.parameters(), lr=.01, weight_decay=1e-5)
lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=4, gamma=.1)

edge_to, edge_from, probs, feature_vectors, attention = construct_spatial_temporal_complex(data_provider, selected_idxs, TIME_STEPS, NUMS, TEMPORAL_PERSISTENT, TEMPORAL_EDGE_WEIGHT)

dataset = DataHandler(edge_to, edge_from, feature_vectors, attention)
n_samples = int(np.sum(NUMS * probs) // 1)

# chosse sampler based on the number of dataset
if len(edge_to) > 2^24:
    sampler = CustomWeightedRandomSampler(probs, n_samples, replacement=True)
else:
    sampler = WeightedRandomSampler(probs, n_samples, replacement=True)

edge_loader = DataLoader(dataset, batch_size=1000, sampler=sampler)
trainer = SingleVisTrainer(model, criterion, optimizer, edge_loader=edge_loader, DEVICE=DEVICE)
patient = PATIENT
for epoch in range(EPOCH_NUMS):
    print("====================\nepoch:{}\n===================".format(epoch))
    prev_loss = trainer.loss
    loss = trainer.train_step()
    # early stop, check whether converge or not
    if prev_loss - loss < 1E-2:
        if patient == 0:
            break
        else:
            patient -= 1
    else:
        patient = PATIENT

time_end = time.time()
time_spend = time_end - time_start
print("Time spend: {:.2f}".format(time_spend))
trainer.save(save_dir=data_provider.model_path, file_name="SV")
trainer.load(file_path=os.path.join(data_provider.model_path,"SV.pth"))

########################################################################################################################
# evaluate
########################################################################################################################
from singleVis.eval.evaluator import Evaluator
evaluator = Evaluator(data_provider, trainer)
evaluator.save_eval(n_neighbors=15, file_name="evaluation")
