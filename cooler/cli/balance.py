# -*- coding: utf-8 -*-
from __future__ import division, print_function
from multiprocess import Pool
import sys

import numpy as np
import pandas as pd
import h5py

import click
from . import cli, logger
from ..api import Cooler
from .. import ice
from ..io import parse_cooler_uri


@cli.command()
@click.argument(
    "cool_uri",
    type=str) #click.Path(exists=True))
@click.option(
    "--nproc", "-p",
    help="Number of processes to split the work between.",
    type=int,
    default=8,
    show_default=True)
@click.option(
    "--chunksize", "-c",
    help="Control the number of pixels handled by each worker process at a time.",
    type=int,
    default=int(10e6),
    show_default=True)
@click.option(
    "--mad-max",
    help="Ignore bins from the contact matrix using the 'MAD-max' filter: "
         "bins whose log marginal sum is less than ``mad-max`` mean absolute "
         "deviations below the median log marginal sum of all the bins in the "
         "same chromosome.",
    type=int,
    default=5,
    show_default=True)
@click.option(
    "--min-nnz",
    help="Ignore bins from the contact matrix whose marginal number of "
         "nonzeros is less than this number.",
    type=int,
    default=10,
    show_default=True)
@click.option(
    "--min-count",
    help="Ignore bins from the contact matrix whose marginal count is less "
         "than this number.",
    type=int,
    default=0,
    show_default=True)
@click.option(
    "--blacklist",
    help="Path to a 3-column BED file containing genomic regions to mask "
         "out during the balancing procedure, e.g. sequence gaps or regions "
         "of poor mappability.",
    type=click.Path(exists=True))
@click.option(
    "--ignore-diags",
    help="Number of diagonals of the contact matrix to ignore, including the "
         "main diagonal. Examples: 0 ignores nothing, 1 ignores the main "
         "diagonal, 2 ignores diagonals (-1, 0, 1), etc.",
    type=int,
    default=2,
    show_default=True)
@click.option(
    "--tol",
    help="Threshold value of variance of the marginals for the algorithm to "
         "converge.",
    type=float,
    default=1e-5,
    show_default=True)
@click.option(
    "--max-iters",
    help="Maximum number of iterations to perform if convergence is not achieved.",
    type=int,
    default=200,
    show_default=True)
@click.option(
    "--cis-only",
    help="Calculate weights against intra-chromosomal data only instead of "
         "genome-wide.",
    is_flag=True,
    default=False)
@click.option(
    "--name",
    help="Name of column to write to.",
    type=str,
    default='weight',
    show_default=True)
@click.option(
    "--force", "-f",
    help="Overwrite the target dataset, 'weight', if it already exists.",
    is_flag=True,
    default=False)
@click.option(
    "--check",
    help="Check whether a data column 'weight' already exists.",
    is_flag=True,
    default=False)
@click.option(
    "--stdout",
    help="Print weight column to stdout instead of saving to file.",
    is_flag=True,
    default=False)
def balance(cool_uri, nproc, chunksize, mad_max, min_nnz, min_count, blacklist,
            ignore_diags, tol, cis_only, max_iters, name, force, check, stdout):
    """
    Out-of-core contact matrix balancing.

    Assumes uniform binning. See the help for various filtering options to
    ignore poorly mapped bins.

    COOL_PATH : Path to a COOL file.

    """
    cool_path, group_path = parse_cooler_uri(cool_uri)

    if check:
        with h5py.File(cool_path, 'r') as h5:
            grp = h5[group_path]
            if name not in grp['bins']:
                click.echo("{}: No '{}' column found.".format(cool_path, name))
                sys.exit(1)
            else:
                click.echo("{}::{} is balanced.".format(cool_path, group_path))
                sys.exit(0)

    with h5py.File(cool_path, 'r+') as h5:
        grp = h5[group_path]
        if name in grp['bins'] and not stdout:
            if not force:
                print("'{}' column already exists. ".format(name) +
                      "Use --force option to overwrite.", file=sys.stderr)
                sys.exit(1)
            else:
                del grp['bins'][name]

    clr = Cooler(cool_uri)

    if blacklist is not None:
        import csv
        with open(blacklist, 'rt') as f:
            bad_regions = pd.read_csv(
                blacklist, 
                sep='\t', 
                header=0 if csv.Sniffer().has_header(f.read(1024)) else None,
                usecols=[0, 1, 2], 
                names=['chrom', 'start', 'end'])
        bins_grouped = clr.bins()[:].groupby('chrom')
        chromsizes = clr.chromsizes
        
        bad_bins = []
        for _, reg in bad_regions.iterrows():
            result = util.bedslice(bins_grouped, chromsizes, 
                                   (reg.chrom, reg.start, reg.end))
            bad_bins.append(result.index.values)
        bad_bins = np.concatenate(bad_bins)
    else:
        bad_bins = None

    try:
        pool = Pool(nproc)
        bias, stats = ice.iterative_correction(
            clr,
            chunksize=chunksize,
            cis_only=cis_only,
            tol=tol,
            min_nnz=min_nnz,
            min_count=min_count,
            blacklist=bad_bins,
            mad_max=mad_max,
            max_iters=max_iters,
            ignore_diags=ignore_diags,
            rescale_marginals=True,
            use_lock=False,
            map=pool.imap_unordered)
    finally:
        pool.close()

    if stdout:
        pd.Series(bias).to_string(
            sys.stdout,
            header=False,
            index=False,
            na_rep='',
            float_format='%g')
    else:
        with h5py.File(cool_path, 'r+') as h5:
            grp = h5[group_path]
            # add the bias column to the file
            h5opts = dict(compression='gzip', compression_opts=6)
            grp['bins'].create_dataset(name, data=bias, **h5opts)
            grp['bins'][name].attrs.update(stats)

