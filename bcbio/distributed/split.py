"""Split files or tasks for distributed processing across multiple machines.

This tackles parallel work within the context of a program, where we split
based on input records like fastq or across regions like chromosomes in a
BAM file. Following splitting, individual records and run and then combined
back into a summarized output file.

This provides a framework for that process, making it easier to utilize with
splitting specific code.
"""
import copy
import collections

def grouped_parallel_split_combine(args, split_fn, group_fn, parallel_fn,
                                   parallel_name, ungroup_name, combine_name,
                                   file_key, combine_arg_keys,
                                   split_outfile_i=-1):
    """Parallel split runner that allows grouping of samples during processing.

    This builds on parallel_split_combine to provide the additional ability to
    group samples and subsequently split them back apart. This allows analysis
    of related samples together. In addition to the arguments documented in
    parallel_split_combine, this takes:

    group_fn: A function that groups samples together given their configuration
      details.
    ungroup_name: Name of a parallelizable function, defined in distributed.tasks,
      that will pull apart results from grouped analysis into individual sample
      results to combine via `combine_name`
    """
    split_args, combine_map, finished_out, extras = _get_split_tasks(args, split_fn, file_key,
                                                                     split_outfile_i)
    grouped_args, grouped_info = group_fn(split_args)
    split_output = parallel_fn(parallel_name, grouped_args)
    ready_output, grouped_output = _check_group_status(split_output, grouped_info)
    ungrouped_output = parallel_fn(ungroup_name, grouped_output)
    final_output = ready_output + ungrouped_output
    combine_args, final_args = _organize_output(final_output, combine_map,
                                                file_key, combine_arg_keys)
    parallel_fn(combine_name, combine_args)
    out = _add_combine_extras(finished_out + final_args, extras)
    return out

def _check_group_status(xs, grouped_info):
    """Identify grouped items that need ungrouping to continue.
    """
    ready = []
    grouped = []
    for x in xs:
        if "group" in x:
            x["group_orig"] = grouped_info[x["group"]]
            grouped.append([x])
        else:
            ready.append(x)
    return ready, grouped

def parallel_split_combine(args, split_fn, parallel_fn,
                           parallel_name, combine_name,
                           file_key, combine_arg_keys, split_outfile_i=-1):
    """Split, run split items in parallel then combine to output file.

    split_fn: Split an input file into parts for processing. Returns
      the name of the combined output file along with the individual
      split output names and arguments for the parallel function.
    parallel_fn: Reference to run_parallel function that will run
      single core, multicore, or distributed as needed.
    parallel_name: The name of the function, defined in
      bcbio.distributed.tasks/multitasks/ipythontasks to run in parallel.
    combine_name: The name of the function, also from tasks, that combines
      the split output files into a final ready to run file.
    split_outfile_i: the location of the output file in the arguments
      generated by the split function. Defaults to the last item in the list.
    """
    split_args, combine_map, finished_out, extras = _get_split_tasks(args, split_fn, file_key,
                                                                     split_outfile_i)
    split_output = parallel_fn(parallel_name, split_args)
    if combine_name:
        combine_args, final_args = _organize_output(split_output, combine_map,
                                                    file_key, combine_arg_keys)
        parallel_fn(combine_name, combine_args)
    else:
        final_args = _add_combine_info(split_output, combine_map, file_key)
    out = _add_combine_extras(finished_out + final_args, extras)
    return out

# ##  Handle information for future combinations

def _add_combine_info(output, combine_map, file_key):
    """Do not actually combine, but add details for later combining work.
    """
    out = []
    for data in output:
        cur_file = data[file_key]
        if not "combine" in data:
            data["combine"] = {}
        data["combine"][file_key] = {"out": combine_map[cur_file],
                                     "extras": []}
        out.append([data])
    return out

def _get_combine_key(data):
    assert len(data["combine"].keys()) == 1
    return data["combine"].keys()[0]

def _add_combine_parts(args, cur_out, data):
    """Add additional parts to existing combine info when combining via another key.
    """
    base_data = args[cur_out]
    file_key = _get_combine_key(base_data)
    assert base_data["combine"][file_key]["out"] == data["combine"][file_key]["out"]
    base_data["combine"][file_key]["extras"].append(data[file_key])
    args[cur_out] = base_data
    return args

def group_combine_parts(xs):
    """Group together a set of files which have been split but are not processed.

    This occurs when doing bam preparation but not variant calling and helps merge
    the combination information back together for later combining.
    """
    file_key = _get_combine_key(xs[0])
    group_by_out = collections.defaultdict(list)
    for x in xs:
        group_by_out[x["combine"][file_key]["out"]].append(x)
    out = []
    for combined_xs in group_by_out.values():
        base = combined_xs[0]
        for combined_x in combined_xs:
            base["combine"][file_key]["extras"].append(combined_x[file_key])
        out.append([base])
    return out

def _add_combine_extras(args, extras):
    """Add in extra combination items: brings along non-processed items.
    """
    if len(extras) == 0:
        return args
    # Pass along extras when we have no processed items
    if len(args) == 0:
        return [[x] for x in extras]
    extra_out_map = collections.defaultdict(list)
    file_key = _get_combine_key(args[0][0])
    out = []
    no_combine_extras = 0
    for extra in extras:
        if "combine" in extra:
            extra_out_map[extra["combine"][file_key]["out"]].append(extra)
        else:
            no_combine_extras += 1
            out.append([extra])
    added = 0
    for arg in (x[0] for x in args):
        cur_out = arg["combine"][file_key]["out"]
        to_add = [x[file_key] for x in extra_out_map[cur_out]]
        added += len(to_add)
        arg["combine"][file_key]["extras"].extend(to_add)
        out.append([arg])
    assert added + no_combine_extras >= len(extras), (added, len(extras))
    return out

def _get_extra_args(extra_args, arg_keys):
    """Retrieve extra arguments to pass along to combine function.

    Special cases like reference files and configuration information
    are passed as single items, the rest as lists mapping to each data
    item combined.
    """
    # XXX back compatible hack -- should have a way to specify these.
    single_keys = set(["sam_ref", "config"])
    out = []
    for i, arg_key in enumerate(arg_keys):
        vals = [xs[i] for xs in extra_args]
        if arg_key in single_keys:
            out.append(vals[-1])
        else:
            out.append(vals)
    return out

def _organize_output(output, combine_map, file_key, combine_arg_keys):
    """Combine output details for parallelization.

    file_key is the key name of the output file used in merging. We extract
    this file from the output data.

    combine_arg_keys are extra items to pass along to the combine function.
    """
    out_map = collections.defaultdict(list)
    extra_args = collections.defaultdict(list)
    final_args = {}
    already_added = []
    extras = []
    for data in output:
        cur_file = data.get(file_key)
        if cur_file:
            cur_out = combine_map[cur_file]
            out_map[cur_out].append(cur_file)
            extra_args[cur_out].append([data[x] for x in combine_arg_keys])
            data[file_key] = cur_out
            if cur_out not in already_added:
                already_added.append(cur_out)
                final_args[cur_out] = data
            elif "combine" in data:
                final_args = _add_combine_parts(final_args, cur_out, data)
            else:
                extras.append([data])
        else:
            extras.append([data])
    combine_args = [[v, k] + _get_extra_args(extra_args[k], combine_arg_keys)
                    for (k, v) in out_map.iteritems()]
    return combine_args, [[final_args[x]] for x in already_added] + extras

def _get_split_tasks(args, split_fn, file_key, outfile_i=-1):
    """Split up input files and arguments, returning arguments for parallel processing.

    outfile_i specifies the location of the output file in the arguments to
    the processing function. Defaults to the last item in the list.
    """
    split_args = []
    combine_map = {}
    finished_order = []
    finished_map = {}
    extras = []
    for data in args:
        out_final, out_parts = split_fn(*data)
        for parts in out_parts:
            split_args.append(copy.deepcopy(data) + list(parts))
        for part_file in [x[outfile_i] for x in out_parts]:
            combine_map[part_file] = out_final
        if len(out_parts) == 0:
            if out_final is not None:
                if out_final not in finished_order:
                    finished_order.append(out_final)
                    data[0][file_key] = out_final
                    finished_map[out_final] = data[0]
                else:
                    finished_map = _add_combine_parts(finished_map, out_final, data[0])
            else:
                assert len(data) == 1
                extras.append(data[0])
    finished_out = [[finished_map[x]] for x in finished_order]
    return split_args, combine_map, finished_out, extras
