# python2, 3 compatibility
from __future__ import absolute_import, division, print_function
import six
import os
import inspect
from builtins import str, open, range, dict

import dill as pickle
import numpy as np
import pandas as pd
import pybedtools
from pybedtools import BedTool

from genomelake.extractors import BaseExtractor, FastaExtractor, one_hot_encode_sequence, NUM_SEQ_CHARS
from pysam import FastaFile
from concise.preprocessing.splines import encodeSplines
from concise.utils.position import extract_landmarks, read_gtf, ALL_LANDMARKS
from kipoi.metadata import GenomicRanges

from kipoi.data import Dataset

filename = inspect.getframeinfo(inspect.currentframe()).filename
DATALOADER_DIR = os.path.dirname(os.path.abspath(filename))


class DistanceTransformer:
    """Transforms the raw distances to the appropriate modeling form
    """

    def __init__(self, pos_features, pipeline_obj_path):
        """
        Args:
          pos_features: list of positional features to use
          pipeline_obj_path: path to the serialized pipeline obj_path
        """
        self.pos_features = pos_features
        self.pipeline_obj_path = pipeline_obj_path

        # deserialize the pickle file
        with open(self.pipeline_obj_path, "rb") as f:
            pipeline_obj = pickle.load(f)
        self.POS_FEATURES = pipeline_obj[0]
        self.preproc_pipeline = pipeline_obj[1]
        self.imp = pipeline_obj[2]

        # for simplicity, assume all current pos_features are the
        # same as from before
        assert self.POS_FEATURES == self.pos_features

    def transform(self, x):
        # impute missing values and rescale the distances
        xnew = self.preproc_pipeline.transform(self.imp.transform(x))

        # convert distances to spline bases
        dist = {"dist_" + k: encodeSplines(xnew[:, i, np.newaxis], start=0, end=1, warn=False)
                for i, k in enumerate(self.POS_FEATURES)}
        return dist


class DistToClosestLandmarkExtractor(BaseExtractor):
    """Extract distances to the closest genomic landmark

    # Arguments
        gtf_file: Genomic annotation file path (say gencode gtf)
        landmarks: List of landmarks to extract. See `concise.utils.position.extract_landmarks`
        use_strand: Take into account the strand of the intervals
    """
    multiprocessing_safe = True

    def __init__(self, gtf_file, landmarks=ALL_LANDMARKS, use_strand=True, **kwargs):
        super(DistToClosestLandmarkExtractor, self).__init__(gtf_file, **kwargs)
        self._gtf_file = gtf_file
        self.landmarks = extract_landmarks(gtf_file, landmarks=landmarks)
        self.columns = landmarks  # column names. Reqired for concating distances into array
        self.use_strand = use_strand

        # set index to chromosome and strand - faster access
        self.landmarks = {k: v.set_index(["seqnames", "strand"])
                          for k, v in six.iteritems(self.landmarks)}

    def _extract(self, intervals, out, **kwargs):

        def find_closest(ldm, interval, use_strand=True):
            """Uses
            """
            # subset the positions to the appropriate strand
            # and extract the positions
            ldm_positions = ldm.loc[interval.chrom]
            if use_strand and interval.strand != ".":
                ldm_positions = ldm_positions.loc[interval.strand]
            ldm_positions = ldm_positions.position.values

            int_midpoint = (interval.end + interval.start) // 2
            dist = (ldm_positions - 1) - int_midpoint  # -1 for 0, 1 indexed positions
            if use_strand and interval.strand == "-":
                dist = - dist

            return dist[np.argmin(np.abs(dist))]

        out[:] = np.array([[find_closest(self.landmarks[ldm_name], interval, self.use_strand)
                            for ldm_name in self.columns]
                           for interval in intervals], dtype=float)

        return out

    def _get_output_shape(self, num_intervals, width):
        return (num_intervals, len(self.columns))


class TxtDataset(Dataset):

    def __init__(self, path):
        with open(path, "r") as f:
            self.lines = f.readlines()

    def __len__(self):
        return len(self.lines)

    def __getitem__(self, idx):
        return int(self.lines[idx].strip())


# --------------------------------------------
class SeqDistDataset(Dataset):
    """
    Args:
        intervals_file: file path; tsv file
            Assumes bed-like `chrom start end id score strand` format.
        fasta_file: file path; Genome sequence
        gtf_file: file path; Genome annotation GTF file.
        preproc_transformer: file path; tranformer used for pre-processing.
        target_file: file path; path to the targets
        batch_size: int
    """

    def __init__(self, intervals_file, fasta_file, gtf_file, target_file=None):
        gtf = read_gtf(gtf_file)
        self.gtf = gtf[gtf["info"].str.contains('gene_type "protein_coding"')]

        # intervals
        self.bt = pybedtools.BedTool(intervals_file)

        # extractors
        self.seq_extractor = FastaExtractor(fasta_file)
        self.dist_extractor = DistToClosestLandmarkExtractor(gtf_file=self.gtf,
                                                             landmarks=ALL_LANDMARKS)

        # here the DATALOADER_DIR contains the path to the current directory
        self.dist_transformer = DistanceTransformer(ALL_LANDMARKS,
                                                    DATALOADER_DIR + "/dataloader_files/position_transformer.pkl")

        # target
        if target_file:
            self.target_dataset = TxtDataset(target_file)
            assert len(self.target_dataset) == len(self.bt)
        else:
            self.target_dataset = None

    def __len__(self):
        return len(self.bt)

    def __getitem__(self, idx):
        interval = self.bt[idx]

        out = {}
        out['inputs'] = {}
        # input - sequence
        out['inputs']['seq'] = np.squeeze(self.seq_extractor([interval]), axis=0)

        # input - distance
        dist_dict = self.dist_transformer.transform(self.dist_extractor([interval]))
        dist_dict = {k: np.squeeze(v, axis=0) for k, v in dist_dict.items()}  # squeeze the batch axis
        out['inputs'] = {**out['inputs'], **dist_dict}

        # targets
        if self.target_dataset is not None:
            out["targets"] = np.array([self.target_dataset[idx]])

        # metadata
        out['metadata'] = {}
        out['metadata']['ranges'] = GenomicRanges.from_interval(interval)

        return out


def test_dataset():
    """Runs tests on the function
    """
    # File paths
    intervals_file = "example_files/intervals.tsv"
    target_file = "example_files/targets.tsv"
    gtf_file = "example_files/gencode.v24.annotation_chr22.gtf"
    fasta_file = "example_files/hg38_chr22.fa"
    ds = SeqDistDataset(intervals_file, fasta_file, gtf_file, target_file)

    ds[0]
    ds[10]
    it = ds.batch_iter(32)
    next(it)
