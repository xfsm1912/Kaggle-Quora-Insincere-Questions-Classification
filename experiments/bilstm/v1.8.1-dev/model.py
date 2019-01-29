from copy import deepcopy

import torch
import numpy as np
from torch import nn

from qiqc.builder import build_attention
from qiqc.builder import build_aggregator
from qiqc.builder import build_encoder
from qiqc.features import WordFeatureTransformer
from qiqc.models import BinaryClassifier


def build_sampler(batchsize, i_cv, epoch, weights):
    return None


def build_models(config, vocab, pretrained_vectors, df):
    models = []
    pos_weight = torch.FloatTensor([config['pos_weight']]).to(config['device'])
    external_vectors = np.stack(
        [wv.vectors for wv in pretrained_vectors.values()])
    embeddings = {'external': external_vectors.mean(axis=0)}
    extra = None
    transformer = WordFeatureTransformer(
        vocab, embeddings['external'], config['vocab']['min_count'])

    # Fine-tuning
    if config['feature']['word']['finetune']['skipgram'] is not None:
        embeddings['skipgram'] = transformer.finetune_skipgram(
            df, config['feature']['word']['finetune']['skipgram'],
            config['feature']['word']['fill']['finetune_unk'])
    if config['feature']['word']['finetune']['fasttext'] is not None:
        embeddings['fasttext'] = transformer.finetune_fasttext(
            df, config['feature']['word']['finetune']['fasttext'],
            config['feature']['word']['fill']['finetune_unk'])

    # Standardize
    assert config['feature']['word']['standardize'] in {'vocab', 'freq', None}
    if config['feature']['word']['standardize'] == 'vocab':
        embeddings = {k: transformer.standardize(v)
                      for k, v in embeddings.items()}
    elif config['feature']['word']['standardize'] == 'freq':
        embeddings = {k: transformer.standardize_freq(v)
                      for k, v in embeddings.items()}

    # Extra features
    if config['feature']['word']['extra'] is not None:
        extra = transformer.prepare_extra_features(
            df, vocab.token2id, config['feature']['word']['extra'])

    for i in range(config['cv']):
        _embeddings = deepcopy(embeddings)
        indices = transformer.unk & transformer.hfq
        _embeddings['external'][indices] = transformer.build_fillvalue(
            config['feature']['word']['fill']['unk_hfq'], indices.sum())

        embedding_matrix = np.stack(list(_embeddings.values())).mean(axis=0)
        embedding_matrix[transformer.lfq & transformer.unk] = 0

        if config['feature']['word']['extra'] is not None:
            embedding_matrix = np.concatenate(
                [embedding_matrix, extra], axis=1)

        embedding = nn.Embedding.from_pretrained(
            torch.Tensor(embedding_matrix), freeze=True)
        lossfunc = nn.BCEWithLogitsLoss(pos_weight=pos_weight)
        model = build_model(config, embedding, lossfunc)
        models.append(model)

    return models, transformer.unk


def build_model(config, embedding, lossfunc):
    encoder = Encoder(config['model'], embedding)
    clf = BinaryClassifier(config['model'], encoder, lossfunc)
    return clf


class Encoder(nn.Module):

    def __init__(self, config, embedding):
        super().__init__()
        self.config = config
        self.embedding = embedding
        config['encoder']['n_input'] = self.embedding.embedding_dim
        if self.config['embed']['dropout1d'] > 0:
            self.dropout1d = nn.Dropout(config['embed']['dropout1d'])
        if self.config['embed']['dropout2d'] > 0:
            self.dropout2d = nn.Dropout2d(config['embed']['dropout2d'])
        self.encoder = build_encoder(
            config['encoder']['name'])(config['encoder'])
        self.aggregator = build_aggregator(
            config['encoder']['aggregator'])
        if self.config['encoder'].get('attention') is not None:
            self.attn = build_attention(config['encoder']['attention'])(
                config['encoder']['n_hidden'] * config['encoder']['out_scale'])
        self.out_size = config['encoder']['n_extra_features'] + \
            config['encoder']['out_scale'] * config['encoder']['n_hidden'] 

    def forward(self, X, X2, mask):
        h = self.embedding(X)
        if self.config['embed']['dropout1d'] > 0:
            h = self.dropout1d(h)
        if self.config['embed']['dropout2d'] > 0:
            h = self.dropout2d(h)
        h = self.encoder(h, mask)
        if self.config['encoder'].get('attention') is not None:
            h = self.attn(h, mask)
        h = self.aggregator(h, mask)
        if self.out_size > 0:
            h = torch.cat([h, X2], dim=1)
        return h
