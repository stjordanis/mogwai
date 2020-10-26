from argparse import ArgumentParser, Namespace
from typing import Optional
import math

import torch
import torch.nn as nn
from apex.optimizers import FusedLAMB

from .base_model import BaseModel
from ..utils import symmetrize_matrix_
from ..utils.init import init_potts_bias


class FactoredAttention(BaseModel):
    """FactoredAttention Layer.

    Args:
        num_seqs (int): Number of sequences in MSA.
        msa_length (int): Length of MSA.
        msa_counts (tensor): Counts of each amino acid in each position of MSA. Used
            for initialization.
        learning_rate (float): Learning rate for training model.
        vocab_size (int, optional): Alphabet size of MSA.
        true_contacts (tensor, optional): True contacts for family. Used to compute
            metrics while training.
        l2_coeff (int, optional): Coefficient of L2 regularization for all weights.
        use_bias (bool, optional): Whether to include single-site potentials.
    """

    def __init__(
        self,
        num_seqs: int,
        msa_length: int,
        msa_counts: torch.Tensor,
        attention_head_size: int = 16,
        num_attention_heads: int = 32,
        optimizer: str = "adam",
        learning_rate: float = 1e-3,
        vocab_size: int = 20,
        true_contacts: Optional[torch.Tensor] = None,
        l2_coeff: float = 1e-2,
        use_bias: bool = True,
        pad_idx: int = 20,
    ):
        super().__init__(num_seqs, msa_length, learning_rate, vocab_size, true_contacts)
        self.l2_coeff = l2_coeff
        self.use_bias = use_bias
        self.pad_idx = pad_idx
        self.num_seqs = num_seqs
        self.msa_length = msa_length
        self.num_attention_heads = num_attention_heads
        self.attention_head_size = attention_head_size
        self.optimizer = optimizer

        hidden_size = attention_head_size * num_attention_heads

        self.query = nn.Parameter(
            0.01 * torch.randn(msa_length, num_attention_heads, attention_head_size)
        )
        self.key = nn.Parameter(
            0.01 * torch.randn(msa_length, num_attention_heads, attention_head_size)
        )
        self.value = nn.Embedding(vocab_size + 1, hidden_size, padding_idx=pad_idx)
        self.output = nn.Linear(hidden_size, vocab_size, bias=False)

        if self.use_bias:
            bias = init_potts_bias(msa_counts, l2_coeff, num_seqs)
            bias = nn.Parameter(bias, True)
            self.register_parameter("bias", bias)

        self.register_buffer("diag_mask", torch.eye(msa_length) * -10000)

    def forward(self, src_tokens, targets=None):
        batch_size, seqlen = src_tokens.size()
        values = self.value(src_tokens).view(
            batch_size, seqlen, self.num_attention_heads, self.attention_head_size
        )
        attention = torch.einsum("ihd,jhd->hij", self.query, self.key)
        attention = attention / math.sqrt(self.attention_head_size)
        attention = attention + self.diag_mask
        attention = attention.softmax(-1)
        context = torch.einsum("hij,njhd->nihd", attention, values)
        context = context.reshape(
            batch_size, seqlen, self.num_attention_heads * self.attention_head_size
        )
        logits = self.output(context)

        if self.use_bias:
            logits = logits + self.bias

        outputs = (logits, attention)
        if targets is not None:
            loss = nn.CrossEntropyLoss(ignore_index=self.pad_idx, reduction="sum")(
                logits.view(-1, self.vocab_size), targets.view(-1)
            )
            loss = loss / batch_size
            outputs = (loss,) + outputs
        return outputs

    def configure_optimizers(self):
        if self.optimizer == "adam":
            optimizer = torch.optim.AdamW(
                self.parameters(), lr=self.learning_rate, weight_decay=self.l2_coeff
            )
        elif self.optimizer == "lamb":
            optimizer = FusedLAMB(
                self.parameters(),
                lr=self.learning_rate,
                weight_decay=self.l2_coeff,
            )
        else:
            raise ValueError(f"Unrecognized optimizer {self.optimizer}")
        return [optimizer]

    @torch.no_grad()
    def get_contacts(self):
        """Extracts contacts by getting the attentions."""
        inputs = torch.full(
            [1, self.msa_length],
            self.pad_idx,
            dtype=torch.long,
            device=next(self.parameters()).device,
        )
        *_, attention = self.forward(inputs)
        attention = attention.mean(0)
        attention = symmetrize_matrix_(attention)
        return attention

    @classmethod
    def from_args(
        cls,
        args: Namespace,
        num_seqs: int,
        msa_length: int,
        msa_counts: torch.Tensor,
        vocab_size: int = 20,
        pad_idx: int = 20,
        true_contacts: Optional[torch.Tensor] = None,
    ) -> "FactoredAttention":
        return cls(
            num_seqs=num_seqs,
            msa_length=msa_length,
            msa_counts=msa_counts,
            attention_head_size=args.attention_head_size,
            num_attention_heads=args.num_attention_heads,
            optimizer=args.optimizer,
            learning_rate=args.learning_rate,
            vocab_size=vocab_size,
            true_contacts=true_contacts,
            l2_coeff=args.l2_coeff,
            use_bias=args.use_bias,
            pad_idx=pad_idx,
        )

    @staticmethod
    def add_args(parser: ArgumentParser) -> ArgumentParser:
        parser.add_argument(
            "--learning_rate",
            type=float,
            default=1e-3,
            help="Learning rate for training.",
        )
        parser.add_argument(
            "--l2_coeff",
            type=float,
            default=1e-2,
            help="L2 Regularization Coefficient.",
        )
        parser.add_argument(
            "--use_bias", action="store_true", help="Use a bias when training GREMLIN."
        )
        parser.add_argument(
            "--no_bias",
            action="store_false",
            help="Use a bias when training GREMLIN.",
            dest="use_bias",
        )
        parser.add_argument(
            "--num_attention_heads",
            type=int,
            default=32,
            help="Number of attention heads.",
        )
        parser.add_argument(
            "--attention_head_size",
            type=int,
            default=16,
            help="Dims in each attention head.",
        )
        parser.add_argument(
            "--optimizer",
            choices=["adam", "lamb"],
            default="adam",
            help="Which optimizer to use.",
        )
        return parser
