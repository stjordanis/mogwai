from argparse import ArgumentParser
import pytorch_lightning as pl
import torch
from pathlib import Path
import numpy as np

from mogwai.data_loading import MSADataModule
from mogwai.parsing import read_contacts
from mogwai import models
from mogwai.utils.functional import apc
from mogwai.metrics import contact_auc
from mogwai.vocab import FastaVocab


def train():
    # Initialize parser
    parser = ArgumentParser()
    parser.add_argument(
        "--model",
        default="gremlin",
        choices=models.MODELS.keys(),
        help="Which model to train.",
    )
    model_name = parser.parse_known_args()[0].model
    parser.add_argument(
        "--structure_file",
        type=str,
        default=None,
        help=(
            "Optional pdb or cf file containing protein structure. "
            "Used for evaluation."
        ),
    )
    parser.add_argument(
        "--output_file",
        type=str,
        default=None,
        help="Optional file to output gremlin weights.",
    )
    parser.add_argument(
        "--contacts_file",
        type=str,
        default=None,
        help="Optional file to output gremlin contacts.",
    )
    parser.add_argument(
        "--wandb_project",
        type=str,
        default=None,
        help="Optional wandb project to log to.",
    )
    parser = MSADataModule.add_args(parser)
    parser = pl.Trainer.add_argparse_args(parser)
    parser.set_defaults(
        gpus=1,
        min_steps=50,
        max_steps=1000,
    )
    model_type = models.get(model_name)
    model_type.add_args(parser)
    args = parser.parse_args()

    # Load msa
    msa_dm = MSADataModule.from_args(args)
    msa_dm.setup()

    # Load contacts
    true_contacts = (
        torch.from_numpy(read_contacts(args.structure_file))
        if args.structure_file is not None
        else None
    )

    # Initialize model
    num_seqs, msa_length, msa_counts = msa_dm.get_stats()
    model = model_type.from_args(
        args,
        num_seqs=num_seqs,
        msa_length=msa_length,
        msa_counts=msa_counts,
        vocab_size=len(FastaVocab),
        pad_idx=FastaVocab.pad_idx,
        true_contacts=true_contacts,
    )

    kwargs = {}
    if args.wandb_project:
        try:
            # Requires wandb to be installed
            logger = pl.loggers.WandbLogger(project=args.wandb_project)
            logger.log_hyperparams(args)
            logger.log_hyperparams(
                {
                    "pdb": Path(args.data).stem,
                    "num_seqs": num_seqs,
                    "msa_length": msa_length,
                }
            )
            kwargs["logger"] = logger
        except ImportError:
            raise ImportError(
                "Cannot use W&B logger w/o W&b install. Run `pip install wandb` first."
            )

    # Initialize Trainer
    trainer = pl.Trainer.from_argparse_args(args, checkpoint_callback=False, **kwargs)

    trainer.fit(model, msa_dm)

    if true_contacts is not None:
        contacts = model.get_contacts()
        auc = contact_auc(contacts, true_contacts).item()
        contacts = apc(contacts)
        auc_apc = contact_auc(contacts, true_contacts).item()
        print(f"AUC: {auc:0.3f}, AUC_APC: {auc_apc:0.3f}")

        if args.wandb_project:
            import matplotlib.pyplot as plt
            import wandb

            from mogwai.plotting import (
                plot_colored_preds_on_trues,
                plot_precision_vs_length,
            )

            filename = "top_L_contacts.png"
            plot_colored_preds_on_trues(contacts, true_contacts, point_size=5)
            logger.log_metrics({filename: wandb.Image(plt)})
            plt.close()

            filename = "top_L_contacts_apc.png"
            plot_colored_preds_on_trues(apc(contacts), true_contacts, point_size=5)
            logger.log_metrics({filename: wandb.Image(plt)})
            plt.close()

            filename = "precision_vs_L.png"
            plot_precision_vs_length(contacts, true_contacts)
            logger.log_metrics({filename: wandb.Image(plt)})
            plt.close()

    if args.output_file is not None:
        torch.save(model.state_dict(), args.output_file)

    if args.contacts_file is not None:
        contacts = model.get_contacts()
        contacts = apc(contacts)
        x_ind, y_ind = np.triu_indices_from(contacts, 1)
        contacts = contacts[x_ind, y_ind]
        torch.save(contacts, args.contacts_file)



if __name__ == "__main__":
    train()
