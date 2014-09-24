"""Organize samples for coordinated multi-sample processing.

Handles grouping of related families or batches to go through variant
calling simultaneously.
"""
import collections
import copy
import os

from bcbio import broad, utils
from bcbio.distributed.transaction import file_transaction
from bcbio.variation import vcfutils

def group_batches(xs):
    """Group samples into batches for simultaneous variant calling.

    Identify all samples to call together: those in the same batch,
    variant caller and genomic region.
    Pull together all BAM files from this batch and process together,
    Provide details to pull these finalized files back into individual
    expected files.
    """
    singles = []
    batch_groups = collections.defaultdict(list)
    for data, region, out_fname in xs:
        batch = data.get("metadata", {}).get("batch")
        caller = data["config"]["algorithm"].get("variantcaller", "gatk")
        if batch is not None:
            batches = batch if isinstance(batch, (list, tuple)) else [batch]
            for b in batches:
                batch_groups[(b, tuple(region), caller)].append((data, out_fname))
        else:
            singles.append((data, tuple(region), out_fname))
    batches = []
    remap_batches = {}
    for (batch, region, _), xs in batch_groups.iteritems():
        cur_data, cur_fname = xs[0]
        batch_fname = utils.append_stem(cur_fname, "-" + str(batch))
        batch_data = copy.deepcopy(cur_data)
        batch_data["work_bam"] = [x[0]["work_bam"] for x in xs]
        batch_data["work_items"] = [x[0] for x in xs]
        batch_data["group"] = batch_fname
        batches.append((batch_data, region, batch_fname))
        remap_batches[batch_fname] = xs
    return singles + batches, remap_batches

def split_variants_by_sample(data):
    """Split a multi-sample call file into inputs for individual samples.

    For tumor/normal paired analyses, assign the combined file to the
    tumor sample instead of splitting, and remove variant files from the normal.
    """
    config = data["config"]
    vrn_file = data["vrn_file"]
    out = []
    # cancer tumor/normal
    if vcfutils.get_paired_phenotype(data):
        # handle trailing normals, which we don't need to process
        if len(data["group_orig"]) == 1 and vcfutils.get_paired_phenotype(data["group_orig"][0][0]) == "normal":
            sub_data = data["group_orig"][0][0]
            sub_data.pop("vrn_file", None)
            out.append(sub_data)
        else:
            has_tumor = False
            for sub_data, sub_vrn_file in data["group_orig"]:
                paired_phenotype = vcfutils.get_paired_phenotype(sub_data)
                if paired_phenotype == "tumor":
                    has_tumor = True
                    if not os.path.exists(sub_vrn_file):
                        utils.symlink_plus(vrn_file, sub_vrn_file)
                    sub_data["vrn_file"] = sub_vrn_file
                    out.append(sub_data)
                else:
                    sub_data.pop("vrn_file", None)
                    out.append(sub_data)
            if not has_tumor:
                raise ValueError("Did not find tumor sample in paired analysis")
    # population or single sample
    else:
        for sub_data, sub_vrn_file in data["group_orig"]:
            if is_multisample(vrn_file):
                select_sample_from_vcf(vrn_file, sub_data["name"][-1], sub_vrn_file,
                                       data["sam_ref"], config)
            elif not os.path.exists(sub_vrn_file):
                utils.symlink_plus(vrn_file, sub_vrn_file)
            if sub_vrn_file:
                sub_data["vrn_file"] = sub_vrn_file
                out.append(sub_data)
    return out

def select_sample_from_vcf(in_file, sample, out_file, ref_file, config):
    """Select a single sample from the supplied multisample VCF file.
    """
    brunner = broad.runner_from_config(config)
    if not utils.file_exists(out_file):
        with file_transaction(out_file) as tx_out_file:
            params = ["-T", "SelectVariants",
                      "-R", ref_file,
                      "--sample_name", sample,
                      "--variant", in_file,
                      "--out", tx_out_file]
            brunner.run_gatk(params)
    return out_file

def is_multisample(fname):
    """Check VCF header to determine if we have a multi-sample file.
    """
    with open(fname) as in_handle:
        for line in in_handle:
            if line.startswith("#CHROM"):
                return len(line.split("\t")) > 10
