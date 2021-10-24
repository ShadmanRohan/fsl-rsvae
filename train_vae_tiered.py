import torch
import argparse
import numpy as np
from torch.autograd import Variable
from torchvision.datasets.folder import DatasetFolder
from torch.distributions import uniform, normal
import torch.nn as nn
import torch.nn.functional as F
from matplotlib import pyplot as plt
from sklearn.preprocessing import MinMaxScaler, StandardScaler
import torch.optim
import json
import torch.utils.data.sampler
import os
import glob
import random
import time
import pdb
import yaml
import datasets.feature_loader as feat_loader
from sklearn.manifold import TSNE
import h5py
from scipy.stats import multivariate_normal
from sklearn.decomposition import PCA
import scipy
from scipy.io import savemat, loadmat
def finetune_vae(feats_vae, x_shot, label_real):
    attributes = np.load('./mini_attr.npy')
    x_shot = x_shot.detach()
    z_dist = normal.Normal(0, 1)
    bs_list = np.arange(4)
    feats_vae.train()
    optimizer = torch.optim.Adam(feats_vae.parameters(), lr=0.0001)
    for ep in range(5):
      np.random.shuffle(bs_list)
      for idx in bs_list:
        targets = x_shot[idx]
        labels_sel = label_real[idx] + 80
        attr = torch.from_numpy(attributes[labels_sel]).float().cuda()
        attr = attr.repeat((1, 50)).reshape((5, 50, -1))
        Z = z_dist.sample((5, 50, 512)).cuda()
        concat_feats = torch.cat((Z, attr), dim=2)
        concat_feats = torch.autograd.Variable(concat_feats, requires_grad=True)
        feats = feats_vae.model(concat_feats).reshape((-1, 512))
        feats = feats_vae.relu(feats_vae.bn1(feats)).reshape((5, 50, 512))
        feats = feats.mean(1)
        feats = F.normalize(feats, dim=-1)
        mse_loss = F.mse_loss(feats, targets)
        optimizer.zero_grad()
        mse_loss.backward()
        optimizer.step()
        print(mse_loss.item())
    pdb.set_trace()



def shrink_feats(cl_data_file):
    cl_mean_file = {}
    weight_data_file = {}
    for k, v in cl_data_file.items():
        mean_feats = np.mean(v, 0)
        cl_mean_file[k] = mean_feats / np.sqrt(np.sum(mean_feats*mean_feats))
    for k, v in cl_data_file.items():
        v = np.array(v)
        v = v / np.sqrt(np.sum(v*v, -1, keepdims=True))
        dist = np.sum((v - cl_mean_file[k])**2, 1)
        sort_idx = np.argsort(dist)
        in_idx = sort_idx[:50]
        out_idx = sort_idx[50:200]
        in_feats = v[in_idx]
        out_feats = v[out_idx]
        cl_data_file[k] = []
        for in_f in in_feats:
          cl_data_file[k].append(in_f)
        for o_idx in out_idx:
          close_feats = (v[o_idx] + cl_mean_file[k]) / 2
          close_dist = np.sum((in_feats - close_feats)**2, -1)
          min_idx = np.argsort(close_dist)[0]
          cl_data_file[k].append(in_feats[min_idx])
    pdb.set_trace()
    return cl_data_file

def det(matrix):
    order=len(matrix)
    posdet=0
    for i in range(order):
        posdet+=reduce((lambda x, y: x * y), [matrix[(i+j)%order][j] for j in range(order)])
    negdet=0
    for i in range(order):
        negdet+=reduce((lambda x, y: x * y), [matrix[(order-i-j)%order][j] for j in range(order)])
    return posdet-negdet


def remove_feats(cl_data_file):
    prob_dict = loadmat('prob_matrix_tiered.mat')
    for k, v in cl_data_file.items():
        prob = prob_dict[str(k)]
        prob_idx = np.where(prob>0.05)[0]
        cl_data_file[k] = []
        for idx in prob_idx:
          cl_data_file[k].append(v[idx])
    pdb.set_trace()
    return cl_data_file

def interpolate_feats(cl_data_file):
    cl_mean_file = {}
    scaler = StandardScaler()
    #for k, v in cl_data_file.items():
    #    cl_mean_file[k] = mean_feats
    for k, v in cl_data_file.items():
        v = np.array(v)
        mean_feats = np.mean(v, 0)
        #v = v / np.sqrt(np.sum(v*v, 1, keepdims=True))
        dist = np.sum((v - mean_feats)**2, 1)
        sort_idx = np.argsort(dist)
        cl_data_file[k] = []
        for iv in sort_idx[:800]:
          cl_data_file[k].append(v[iv])

    return cl_data_file

def pca_feats(cl_data_file):
    cl_mean_file = {}
    scaler = StandardScaler()
    #for k, v in cl_data_file.items():
    #    cl_mean_file[k] = mean_feats
    for k, v in cl_data_file.items():
        v = np.array(v)
        pca = PCA(n_components=250)
        v2 = pca.fit_transform(v)
        cl_data_file[k] = []
        v = pca.inverse_transform(v2)
        for iv in v:
          cl_data_file[k].append(iv)

    return cl_data_file

def get_vae_center(out_dir, split='train'):
    attr_out_file = os.path.join(out_dir, '%s_attr_ood.hdf5'%split)
    vae_data_file = feat_loader.init_loader(attr_out_file)
    if split == 'train':
      num = 351
    else:
      num = 160
    vae_feats_all = torch.zeros((num, 512))
    mean_data_file = {}

    for k, feats in vae_data_file.items():
        mean_feats = np.mean(feats, 0)
        mean_data_file[k] = mean_feats

    for k, feats in vae_data_file.items():
        #mean_feats = np.array(feats)[:50]
        #mean_feats = 3*mean_feats - 2*mean_data_file[k]
        mean_feats = np.mean(feats, 0)
        mean_feats = torch.from_numpy(mean_feats)
        mean_feats = F.normalize(mean_feats, dim=-1) 
        #if split == 'test': 
        #  k = k - 80
        vae_feats_all[k] = mean_feats
  
    return vae_feats_all

class FeatsVAE(nn.Module):
    def __init__(self, x_dim, latent_dim):
        super(FeatsVAE, self).__init__()
        self.linear = nn.Sequential(
            nn.Linear(x_dim, 4096),
            #nn.LeakyReLU(),
            #nn.Linear(2048, 4096),
            nn.LeakyReLU())
        self.linear_mu =  nn.Sequential(
            nn.Linear(4096, latent_dim),
            nn.ReLU())
        self.linear_logvar =  nn.Sequential(
            nn.Linear(4096, latent_dim),
            nn.ReLU())
        self.model = nn.Sequential(
            nn.Linear(2*latent_dim, 4096),
            nn.LeakyReLU(),
            nn.Linear(4096, x_dim),
            #nn.LeakyReLU(),
            #nn.Linear(2048, x_dim),
            #nn.Sigmoid(),
        )
        self.bn1 = nn.BatchNorm1d(x_dim)
        self.relu = nn.ReLU(inplace=True)
        self.z_dist = normal.Normal(0, 1)
        self.init_weights()


    def reparameterize(self, mu, logvar):
        std = torch.exp(0.5*logvar)  
        eps = torch.randn_like(std)
        # remove abnormal points
        return mu + eps*std

    def init_weights(self):
        for m in self.modules():
          if isinstance(m, nn.Linear):
              m.weight.data.normal_(0, 0.02)
              m.bias.data.normal_(0, 0.02)

    def forward(self, x, attr):
        x = self.linear(x)
        mu = self.linear_mu(x)
        logvar = self.linear_logvar(x)
        latent_feats = self.reparameterize(mu, logvar)
        #Z = self.z_dist.sample(attr.shape).cuda() 
        concat_feats = torch.cat((latent_feats, attr), dim=1)
        recon_feats = self.model(concat_feats)
        recon_feats = self.relu(self.bn1(recon_feats))
        return mu, logvar, recon_feats



class FeatureDataset(DatasetFolder):
    """Face Landmarks dataset."""

    def __init__(self, feature_dict, label_list=[]):
        """
        Args:
            csv_file (string): Path to the csv file with annotations.
            root_dir (string): Directory with all the images.
            transform (callable, optional): Optional transform to be applied
                on a sample.
        """
        self.label_list = label_list
        self.features, self.labels = self.convert_dict_to_list(feature_dict)

    def convert_dict_to_list(self, feature_dict):
        features = []
        labels = []
        for k, v in feature_dict.items():
            if k in self.label_list:
              continue
            features += v
            labels += [k] * len(v)
        return features, labels

    def __len__(self):
        return len(self.features)

    def __getitem__(self, idx):
        return self.features[idx], self.labels[idx]

def generate_feats(feats_vae, attributes, output_file, label_list):
    f = h5py.File(output_file, 'w')
    ind_count = 500
    max_count = ind_count * len(label_list)
    all_labels = f.create_dataset('all_labels',(max_count,), dtype='i')
    all_feats=None
    count=0
    feats_vae.eval()
    z_dist = normal.Normal(0, 1)
    for label in label_list:
        attr = torch.from_numpy(attributes[label]).float().cuda()
        attr = attr.repeat(ind_count, 1)
        Z = z_dist.sample((ind_count, 512)).cuda()
        concat_feats = torch.cat((Z, attr), dim=1)
        feats = feats_vae.model(concat_feats)
        feats = feats_vae.relu(feats_vae.bn1(feats))
        if all_feats is None:      
          all_feats = f.create_dataset('all_feats', [max_count] + list(feats.size()[1:]) , dtype='f')
        all_feats[count:count+feats.size(0)] = feats.data.cpu().numpy()
        all_labels[count:count+feats.size(0)] = np.array([label]*ind_count)
        count = count + feats.size(0)
    count_var = f.create_dataset('count', (1,), dtype='i')
    count_var[0] = count

    f.close()

       

def train_vae(feature_loader, feats_vae, attributes):
    optimizer = torch.optim.Adam(feats_vae.parameters(), lr=0.001)
    #for ep in range(10):
    for ep in range(20):
      loss_recon_all = 0
      loss_kl_all = 0
      for idx, (data, label) in enumerate(feature_loader):
        data = data.cuda()
        data = F.normalize(data, dim=-1)
        #weight = weight.cuda() / torch.sum(weight)
        attr = torch.from_numpy(attributes[label]).float().cuda()
        mu, logvar, recon_feats = feats_vae(data, attr)
        recon_loss = ((recon_feats - data)**2).mean(1)
        recon_loss = torch.mean(recon_loss)
        #kl_loss = -0.5*torch.sum(1+logvar-logvar.exp()-mu.pow(2)) / data.shape[0]
        kl_loss = (1+logvar-logvar.exp()-mu.pow(2)).sum(1)
        kl_loss = -0.5*torch.mean(kl_loss)
        L_vae = recon_loss+kl_loss*0.005
        optimizer.zero_grad()
        L_vae.backward()   
        optimizer.step()
        loss_recon_all += recon_loss.item()
        loss_kl_all += kl_loss.item()
        if idx % 200 == 0:
          print('Ep: %d Idx: %d Recon Loss: %f   KL Loss: %f'%(ep, idx, loss_recon_all/(idx+1), loss_kl_all/(idx+1)))
    return feats_vae
    ##torch.save({'state': feats_vae.state_dict()}, 'feats_vae_mini.pth') 


def visualize_feats(cl_data_file, vae_data_file):
    visual_feats = []
    attr_feats = []
    visual_labels = []
    attr_labels = []
    #labels = [51, 3, 179, 7, 11, 175] 
    #labels = [15,6,17,8,9]
    labels = [85, 86]
    #labels = [13, 17, 21, 29, 33, 37]
    tsne = TSNE(n_components=2, random_state=0)
    for idx in range(2):
        label = labels[idx]
        visual_feats.extend(cl_data_file[label-80][:300])
        attr_feats.extend((vae_data_file[label][:300]))
        #attr_feats.extend(np.mean(np.array(vae_data_file[label]), 0, keepdims=True))
        visual_labels.extend([idx]*len(cl_data_file[label-80][:300]))
        attr_labels.extend([idx]*len(vae_data_file[label][:300]))
        #attr_labels.extend([idx])
    visual_feats = np.array(visual_feats)
    attr_feats = np.array(attr_feats)
    all_feats = np.concatenate((visual_feats, attr_feats), 0)
    feats_len = np.sum(all_feats*all_feats, 1)
    feats_len = feats_len / np.sqrt(feats_len)
    all_feats = all_feats / feats_len.reshape((-1, 1))
    pdb.set_trace()
    all_labels = visual_labels + attr_labels
    all_feats_2D = tsne.fit_transform(all_feats)
    #all_feats_2D = tsne.fit_transform(visual_feats)
    #all_labels = visual_labels
    colors = np.array(['r', 'g', 'b', 'c', 'm', 'y', 'k',  'orange', 'purple'])      
    for idx in range(all_feats_2D.shape[0]):
        feat = all_feats_2D[idx]
        #if feat[0] < -30 or feat[1] < -30:
        #  continue
        label = all_labels[idx]
        color = colors[label]
        if idx < visual_feats.shape[0]:
          marker = '*'
          #continue
        else:
          marker = 'o'
          #continue
        plt.scatter(feat[0], feat[1], c=color, marker=marker) 
    plt.savefig('features_base_mini.png')

def save_vae_features(out_file, attr_out_dir):
    cl_data_file = feat_loader.init_loader(out_file)
    #prob_matrix = remove_feats(cl_data_file)
    #pdb.set_trace()
    cl_data_file = remove_feats(cl_data_file)
    feature_dataset = FeatureDataset(cl_data_file)
    feature_loader = torch.utils.data.DataLoader(feature_dataset, shuffle=True, pin_memory=True, drop_last=False, batch_size=256) 
    attributes = np.load('./tiered_attr_train_clip.npy')
    attributes_test = np.load('./tiered_attr_test_clip.npy')
    feats_vae = FeatsVAE(512, 512).cuda()
    feats_vae = train_vae(feature_loader, feats_vae, attributes)
    #torch.save({'state': feats_vae.state_dict()}, 'feats_vae_mini.pth') 
    generate_feats(feats_vae, attributes, os.path.join(attr_out_dir, 'train_attr_ood.hdf5'), np.arange(0, 351))
    generate_feats(feats_vae, attributes_test, os.path.join(attr_out_dir, 'test_attr_ood.hdf5'), np.arange(0, 160))
    #return feats_vae



if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='./configs/test_few_shot.yaml')
    parser.add_argument('--gpu', default='0')
    args = parser.parse_args()

    config = yaml.load(open(args.config, 'r'), Loader=yaml.FullLoader)
    out_dir = os.path.dirname(config['load_encoder'])
    out_file = os.path.join(out_dir, 'features', 'train.hdf5')
    out_dir = os.path.join(out_dir, 'features')
    cl_data_file = feat_loader.init_loader(out_file)
    feature_dataset = FeatureDataset(cl_data_file)
    feature_loader = torch.utils.data.DataLoader(feature_dataset, shuffle=True, pin_memory=True, drop_last=False, batch_size=256)
     
    #attributes = np.load('./mini_attr.npy')
    #feats_vae = FeatsVAE(512, 512).cuda()
    #train_vae(feature_loader, feats_vae, attributes)
    #generate_feats(feats_vae, attributes, os.path.join(out_dir, 'features', 'test_attr.hdf5'), np.arange(80, 100))
    save_vae_features(out_file, out_dir)
    #vae_feats_file = os.path.join(out_dir, 'features', 'test_attr.hdf5')
    #vae_data_file = feat_loader.init_loader(vae_feats_file)
    #visualize_feats(cl_data_file, vae_data_file)