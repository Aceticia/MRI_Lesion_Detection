import torch
from torch.utils.data import DataLoader, Dataset, random_split
import torch.nn as nn
import torch.nn.functional as F
import pytorch_lightning as pl

import os
import numpy as np
import nibabel as nib

from CNN import cnn_multi_dim, output_single
from transformer import MultiViewTransformer
from argparse import ArgumentParser

from torchmetrics import AUROC, MeanAbsoluteError
from pytorch_lightning import Trainer
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.loggers import WandbLogger

class FinetuneModel(pl.LightningModule):
    def __init__(self, args):
        super().__init__()
        self.save_hyperparameters(args)

        # Load transformer, and potentially disable gradient
        self.transformer = MultiViewTransformer(args)
        self.transformer.load_state_dict(torch.load(args.transformer_checkpoint_path))
        if not self.hparams.finetune_transformer: 
            for p in self.transformer.parameters():
                p.requires_grad = False
            self.transformer.eval()

        in_size = self.hparams.n_hidden
        # Load CNN model, and potentially disable gradient
        self.cnn_models = nn.ModuleList([cnn_multi_dim(i, in_size) for i in range(3)])
        loaded = torch.load(args.cnn_checkpoint_path)
        for model_idx, model in enumerate(self.cnn_models):
            model.load_state_dict(loaded[model_idx])
        if not self.hparams.finetune_cnn:
            for p in self.cnn_models.parameters():
                p.requires_grad = False
            self.cnn_models.eval()

        # Output size from transformer is [B, T, 3, C]
        mlps = [
            nn.Linear(in_size, in_size),
            nn.ReLU(),
        ]

        # Classifier or regression
        if self.hparams.classification:
            self.train_metric = AUROC(num_classes=3)
            self.test_metric = AUROC(num_classes=3)
            self.val_metric = AUROC(num_classes=3)
            self.loss = F.nll_loss
            mlps.append(nn.Linear(in_size, 3))
        else:
            self.train_metric = MeanAbsoluteError()
            self.test_metric = MeanAbsoluteError()
            self.val_metric = MeanAbsoluteError()
            self.loss = F.smooth_l1_loss
            mlps += [nn.Linear(in_size, 1), nn.Sigmoid()]

        # Finally instantiating the sequential
        self.mlps = nn.Sequential(*mlps)

    def configure_optimizers(self):
        return torch.optim.AdamW(filter(lambda x: x.requires_grad, self.parameters()), lr=self.hparams.lr)

    def forward(self, x):
        # First pass through cnn 
        out = output_single(self.cnn_models, x).transpose(1, 2)
        
        # Then pass through transformer
        out = self.transformer(out)

        # Average over the slice dimension
        out = out.flatten(1, 2).mean(1)

        # Finally through the MLP
        out = self.mlps(out)

        # adjust range to (0, 100) if we are doing regression
        if not self.hparams.classification:
            out = out*100
        return out

    def get_loss_metrics(self, input, target, stage):
        # First get the output logits or regression output
        pred = self(input)
        pred_metric = pred

        # If we are doing classification, nll loss needs log softmax and auroc needs softmax
        if self.hparams.classification:
            pred_metric = F.softmax(pred, dim=1)
            pred = F.log_softmax(pred, dim=1)
        else:
            pred = pred.flatten()
            pred_metric = pred_metric.flatten()

        # Select the metric
        metric = {
            'train': self.train_metric,
            'test': self.test_metric,
            'val': self.val_metric,
        }[stage]

        # Evaluate metrics and loss
        metric(pred_metric, target)
        loss = self.loss(pred, target)

        # Log the loss and metric for current stage
        self.log(f'{stage}_loss', loss, on_step=True , on_epoch=True)
        self.log(f'{stage}_metric', metric, on_step=True, on_epoch=True)
        return loss

    def training_step(self, batch, _):
        return self.get_loss_metrics(batch[0], batch[1], 'train')

    def test_step(self, batch, _):
        return self.get_loss_metrics(batch[0], batch[1], 'test')

    def validation_step(self, batch, _):
        return self.get_loss_metrics(batch[0], batch[1], 'val')

def customToTensor(img):
    if isinstance(img, np.ndarray):
        img1 = torch.from_numpy(img)
        img1 = resize_image(img, (150, 150, 200))
        # backward compatibility
        return img1.astype(np.float32)

def resize_image(img_array, trg_size):
    res = np.resize(img_array, trg_size)
    # type check
    if type(res) != np.ndarray:
        raise "type error!"
    return res

class ADNIDataset(Dataset):
    def __init__(self, root_dir, data_file, classification=True):
        """
        Args:
            root_dir (string): Directory of all the images.
            data_file (string): File name of the train/test split file.
        """
        self.root_dir = root_dir
        self.lines = [x.split(',') for x in open(data_file).readlines()][1:]
        self.classification = classification
        self.label_file_idx = 2 if classification else 4

    def __len__(self):
        return len(self.lines)

    def __getitem__(self, idx):
        lst = self.lines[idx]
        img_name = lst[0].strip('\"')
        if self.classification:
            img_label = lst[self.label_file_idx].strip('\"')
            if img_label == 'AD':
                label = 1
            elif img_label == 'CN':
                label = 0
            elif img_label == 'MCI':
                label = 2
        else:
            img_label = float(lst[self.label_file_idx].strip('\"'))
            label = img_label
        image_path = f'{os.path.join(self.root_dir, img_name)}.nii'
        image = nib.load(image_path)
        a = (image.get_fdata()) #convert to np array
        a = customToTensor(a)

        return a, label

if __name__ == '__main__':

    parser = ArgumentParser()
    parser.add_argument("--random_seed", type=int, default=0)
    parser.add_argument("--train_batch_size", type=int, default=4)
    parser.add_argument("--eval_batch_size", type=int, default=4)
    parser.add_argument("--adni_dataset", type=str, default='./adni_data')
    parser.add_argument("--csv_file_loc", type=str, default='./ADNI1_Annual_2_Yr_3T_4_23_2022.csv')
    parser.add_argument("--cnn_checkpoint_path", type=str, default='./cnn_checkpoints/checkpointat5.pth')
    parser.add_argument("--transformer_checkpoint_path", type=str, default='./transformer_checkpoints/')
    parser.add_argument("--model_checkpoint_path", type=str, default='./complete_checkpoints/')
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--finetune_cnn", type=int, default=1)
    parser.add_argument("--finetune_transformer", type=int, default=1)
    parser.add_argument("--classification", type=int, default=1)
    parser.add_argument("--n_hidden", type=int, default=10)
    parser.add_argument("--pretrained", type=int, default=0)

    parser.add_argument("--train_ratio", type=float, default=1.)
    parser.add_argument("--val_ratio", type=float, default=0.2)
    parser.add_argument("--test_ratio", type=float, default=0.2)

    # Add trainer specific arguments and parse them
    parser = Trainer.add_argparse_args(parser)
    args = parser.parse_args()

    # Apply random seed
    pl.seed_everything(args.random_seed)

    # First create the entire dataset
    dataset = ADNIDataset(args.adni_dataset, args.csv_file_loc, args.classification)

    # Split the dataset into train, test, and val
    lengths = [int(args.val_ratio*len(dataset)), int(args.test_ratio*len(dataset))]
    if args.train_ratio == 1:
        lengths += [len(dataset)-sum(lengths)]
        val_dataset, test_dataset, train_dataset = random_split(dataset, lengths)
    else:
        lengths += [int((len(dataset)-sum(lengths))*args.train_ratio)]
        lengths += [len(dataset)-sum(lengths)]
        val_dataset, test_dataset, train_dataset, _ = random_split(dataset, lengths)

    # Instantiate the dataloders
    val_dataloader = DataLoader(val_dataset, batch_size=args.eval_batch_size, num_workers=8, shuffle=False)
    test_dataloader = DataLoader(test_dataset, batch_size=args.eval_batch_size, num_workers=8, shuffle=False)
    train_dataloader = DataLoader(train_dataset, batch_size=args.train_batch_size, num_workers=8, shuffle=True)

    # Instantiate the model
    model = FinetuneModel(args)

    # Initialize logger
    wandb_logger = WandbLogger(project="MRI_project")

    # Instantiate the trainer
    trainer = Trainer.from_argparse_args(
        args,
        gpus=2,
        strategy='ddp',
        logger=wandb_logger)

    # Actually train, with early stopping and checkpoint
    trainer.fit(model, train_dataloader, val_dataloader) 

    # Finally, test using the best checkpoint
    trainer.test(model, test_dataloader)

