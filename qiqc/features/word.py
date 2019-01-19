import numpy as np
import pandas as pd
from gensim.corpora import Dictionary
from scipy.stats import chi2_contingency

from qiqc.utils import parallel_apply


class WordFeature(object):

    def __init__(self, word_freq, token2id, pretrained_vectors, min_count):
        self.word_freq = word_freq
        self.token2id = token2id
        self.pretrained_vectors = pretrained_vectors
        self.finetuned_vectors = None

        self.unk = (pretrained_vectors == 0).all(axis=1)
        self.known = ~self.unk
        self.lfq = np.array(list(word_freq.values())) < min_count
        self.hfq = ~self.lfq
        self.mean = pretrained_vectors[self.known].mean()
        self.std = pretrained_vectors[self.known].std()
        self.extra_features = None

    def finetune(self, w2vmodel, df):
        n_embed = self.pretrained_vectors.shape[1]
        tokens = df.tokens.values
        w2v = w2vmodel(
            size=n_embed, min_count=1, workers=1, sorted_vocab=0)
        w2v.build_vocab_from_freq(self.word_freq)
        w2v.wv.vectors[:] = self.pretrained_vectors
        w2v.trainables.syn1neg[:] = self.pretrained_vectors
        w2v.train(tokens, total_examples=len(tokens), epochs=5)
        self.finetuned_vectors = w2v.wv.vectors

    def prepare_extra_features(self, df, token2id, features):
        self.extra_features = np.empty((len(token2id), 0))
        if 'chi2' in features:
            chi2_features = self._prepare_chi2(df, token2id)
            self.extra_features = np.concatenate(
                [self.extra_features, chi2_features], axis=1)

    # TODO: Fix to build dictionary for calculation efficiency
    def _prepare_chi2(self, df, token2id, threshold=0.01):
        df_pos = df[df.target == 1]
        df_neg = df[df.target == 0]
        vocab_pos = Dictionary(df_pos.tokens.values, prune_at=None)
        vocab_neg = Dictionary(df_neg.tokens.values, prune_at=None)
        counts = pd.DataFrame({'tokens': list(token2id.keys())})
        counts['TP'], counts['FP'] = 0, 0

        idxmap = [token2id[vocab_pos[k]] for k, v in vocab_pos.dfs.items()]
        counts.loc[idxmap, 'TP'] = list(vocab_pos.dfs.values())
        idxmap = [token2id[vocab_neg[k]] for k, v in vocab_neg.dfs.items()]
        counts.loc[idxmap, 'FP'] = list(vocab_neg.dfs.values())

        counts['FN'] = len(df_pos) - counts.TP
        counts['TN'] = len(df_neg) - counts.FP
        counts['TP/.P'] = counts.TP / (counts.TP + counts.FP)
        class_ratio = sum(df.target == 1) / len(df)

        def chi2_func(x):
            if x.TN == 0 or x.TP == 0:
                return np.inf
            else:
                return chi2_contingency(np.array(
                    [[x.TP, x.FP], [x.FN, x.TN]]))[1]

        counts['chi2_p'] = parallel_apply(
            counts, lambda x: x.apply(chi2_func, axis=1))
        counts['feature'] = 0
        is_important = (counts.chi2_p < threshold) & \
            (counts['TP/.P'] > class_ratio)
        counts.loc[is_important, 'feature'] = 1
        return counts.feature[:, None]

    def build_feature(self, add_noise=None):
        assert add_noise in {'unk&hfq', None}
        embedding_vectors = self.pretrained_vectors.copy()

        # Assign noise vectors to unknown high frequency tokens
        if add_noise == 'unk&hfq':
            indices = self.unk & self.hfq
            embedding_vectors[indices] += np.random.normal(
                self.mean, self.std, embedding_vectors[indices].shape)
        # Blend external vectors with local finetuned vectors
        if self.finetuned_vectors is not None:
            embedding_vectors += self.finetuned_vectors
            embedding_vectors /= 2

        embedding_vectors[self.lfq & self.unk] = 0

        if self.extra_features is not None:
            embedding_vectors = np.concatenate(
                [embedding_vectors, self.extra_features], axis=1)
        return embedding_vectors
