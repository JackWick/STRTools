#!/usr/bin/env python

"""
Tool for filtering and QC of STR genotypes

Example command:

./dumpSTR.py \
--vcf /storage/mgymrek/ssc-imputation/filtered_vcfs/hipstr.chr22.allfilters.vcf.gz \
--out test \
--min-call-DP 10 \
--max-call-DP 1000 \
--min-call-Q 0.9 \
--max-call-flank-indel 0.15 \
--max-call-stutter 0.15 \
--min-locus-callrate 0.8 \
--min-locus-hwep 0.01 \
--min-locus-het 0 \
--max-locus-het 1 \
--use-length \
--filter-regions filter_files/hg19_segmentalduplications.bed.gz \
--filter-regions-names SEGDUP \
--filter-hrun \
--num-records 10
"""

# TODO:
# - add GangSTR filters
# - add README info

import sys
sys.path.append("../utils")

# Load external libraries
import argparse
import common
import inspect
import sys
import utils
import vcf
from vcf.parser import _Filter
from vcf.parser import _Format
from vcf.parser import _Info

# Load custom libraries
import filters

def CheckFilters(invcf, args):
    """
    Perform checks on user input for filters

    Input:
    - invcf (vcf.Reader)
    - args (argparse namespace)

    Exit program if checks fail
    """
    if args.min_call_DP is not None:
        if args.min_call_DP < 0:
            common.ERROR("Invalid min_call_DP <0")
        if "DP" not in invcf.formats:
            common.ERROR("No DP FORMAT found")
    if args.max_call_DP is not None:
        if args.max_call_DP < 0:
            common.ERROR("Invalid min_call_DP <0")
        if args.min_call_DP is not None and args.max_call_DP <= args.min_call_DP:
            common.ERROR("--max-call-DP must be > --min-call-DP")
        if "DP" not in invcf.formats:
            common.ERROR("No DP FORMAT found")
    if args.min_call_Q is not None:
        if args.min_call_Q < 0 or args.min_call_Q > 1:
            common.ERROR("--min-call-Q must be between 0 and 1")
        if "Q" not in invcf.formats:
            common.ERROR("No Q FORMAT found")
    if args.max_call_flank_indel is not None:
        if args.max_call_flank_indel < 0 or args.max_call_flank_indel > 1:
            common.ERROR("--max-call-flank-indel must be between 0 and 1")
        if "DP" not in invcf.formats or "DFLANKINDEL" not in invcf.formats:
            common.ERROR("No DP or DFLANKINDEL FORMAT found")
    if args.max_call_stutter is not None:
        if args.max_call_stutter < 0 or args.max_call_stutter > 1:
            common.ERROR("--max-call-stutter must be between 0 and 1")
        if "DP" not in invcf.formats or "DSTUTTER" not in invcf.formats:
            common.ERROR("No DP or DSTUTTER FORMAT found")        

def WriteLocLog(loc_info, fname):
    """
    Write locus-level features to log file

    Input:
    - loc_info (dict): dictionary with locus-level stats
    - fname (str): output filename
    """
    f = open(fname, "w")
    keys = list(loc_info.keys())
    keys.remove("totalcalls")
    if loc_info["PASS"] == 0: callrate = 0
    else: callrate = loc_info["totalcalls"]*1.0/loc_info["PASS"]
    f.write("MeanSamplesPerPassingSTR\t%s\n"%callrate)
    for k in keys:
        f.write("FILTER:%s\t%s\n"%(k, loc_info[k]))
    f.close()

def WriteSampLog(sample_info, reasons, fname):
    """
    Write sample-level features to log file

    Input:
    - sample_info (dict): dictionary of stats for each sample
    - reasons (list<str>): list of possible feature reasons
    - fname (str): output filename
    """
    header = ["sample", "numcalls","meanDP"] + reasons
    f = open(fname, "w")
    f.write("\t".join(header)+"\n")
    for s in sample_info:
        numcalls = sample_info[s]["numcalls"]
        if numcalls > 0:
            meancov = sample_info[s]["totaldp"]*1.0/numcalls
        else: meancov = 0
        items = [s, numcalls, meancov]
        for r in reasons: items.append(sample_info[s][r])
        f.write("\t".join([str(item) for item in items])+"\n")
    f.close()

def GetAllCallFilters():
    """
    List all possible call filters by
    extracting from filters module

    Output:
    - reasons (list<str>): list of call-level filter reasons
    """
    reasons = []
    for name, obj in inspect.getmembers(filters):
        if inspect.isclass(obj) and issubclass(obj, filters.Reason) and not obj is filters.Reason:
            reasons.append(obj.name)
    return reasons

def FilterCall(sample, call_filters):
    """
    Apply call-level filters and return filter reason.

    Input:
    - sample (vcf._Call)
    - call_filters (list<filters.Reason>): list of call level filters

    Return:
    - reason (list<str>): list of string description of filter reasons
    """
    reasons = []
    for cfilt in call_filters:
        if cfilt(sample) is not None: reasons.append(cfilt.GetReason())
    return reasons

def ApplyCallFilters(record, reader, call_filters, sample_info):
    """
    Apply call level filters to a record
    Return a new record with FILTER populated for each sample
    Update sample_info with sample level stats

    Input:
    - record (vcf._Record)
    - reader (vcf.Reader)
    - call_filters (list<filters.Reason>): list of call filters
    - sample_info (dict): dictionary of sample stats

    Output:
    - modified record (vcf._Record). 
    """
    if "FILTER" in record.FORMAT:
        samp_fmt = vcf.model.make_calldata_tuple(record.FORMAT.split(':'))
    else: samp_fmt = vcf.model.make_calldata_tuple(record.FORMAT.split(':')+["FILTER"])
    for fmt in samp_fmt._fields:
        if fmt == "FILTER" and "FILTER" not in record.FORMAT:
            samp_fmt._types.append("String")
            samp_fmt._nums.append(1)
        else:
            entry_type = reader.formats[fmt].type
            entry_num  = reader.formats[fmt].num
            samp_fmt._types.append(entry_type)
            samp_fmt._nums.append(entry_num)
    # Get data
    new_samples = []
    for sample in record:
        sampdat = []
        if sample['GT'] is None or sample['GT'] == "./." or sample['GT'] == ".":
            for i in range(len(samp_fmt._fields)):
                key = samp_fmt._fields[i]
                if key == "FILTER":
                    sampdat.append("NOCALL")
                else: sampdat.append(sample[key])
            call = vcf.model._Call(record, sample.sample, samp_fmt(*sampdat))
            new_samples.append(call)
            continue
        filter_reasons = FilterCall(sample, call_filters)
        if len(filter_reasons) > 0:
            for r in filter_reasons:
                sample_info[sample.sample][r] += 1
            for i in range(len(samp_fmt._fields)):
                key = samp_fmt._fields[i]
                if key == "GT":
                    sampdat.append("./.")
                else:
                    if key == "FILTER": sampdat.append(",".join(filter_reasons))
                    else: sampdat.append(None)
        else:
            sample_info[sample.sample]["numcalls"] += 1
            sample_info[sample.sample]["totaldp"] += sample["DP"]
            for i in range(len(samp_fmt._fields)):
                key = samp_fmt._fields[i]
                if key == "FILTER": sampdat.append("PASS")
                else: sampdat.append(sample[key])
        call = vcf.model._Call(record, sample.sample, samp_fmt(*sampdat))
        new_samples.append(call)
    record.samples = new_samples
    return record

def BuildCallFilters(args):
    """
    Build list of locus-level filters to include

    Input:
    - args (namespace from parser.parse_args)
    
    Output:
    - cdict (list<filters.Filter>): list of call-level filters
    """
    cdict = []
    if args.min_call_DP is not None:
        cdict.append(filters.LowCallDepth(args.min_call_DP))
    if args.max_call_DP is not None:
        cdict.append(filters.HighCallDepth(args.max_call_DP))
    if args.min_call_Q is not None:
        cdict.append(filters.LowCallQ(args.min_call_Q))
    if args.max_call_flank_indel is not None:
        cdict.append(filters.CallFlankIndels(args.max_call_flank_indel))
    if args.max_call_stutter is not None:
        cdict.append(filters.CallStutter(args.max_call_stutter))
    return cdict

def BuildLocusFilters(args):
    """
    Build list of locus-level filters to include

    Input:
    - args (namespace from parser.parse_args)
    
    Output:
    - fdict (list<filters.Filter>): list of locus-level filters
    """
    fdict = []
    if args.min_locus_callrate is not None:
        fdict.append(filters.Filter_MinLocusCallrate(args.min_locus_callrate))
    if args.min_locus_hwep is not None:
        fdict.append(filters.Filter_MinLocusHWEP(args.min_locus_hwep, args.use_length))
    if args.min_locus_het is not None:
        fdict.append(filters.Filter_MinLocusHet(args.min_locus_het, args.use_length))
    if args.max_locus_het is not None:
        fdict.append(filters.Filter_MaxLocusHet(args.max_locus_het, args.use_length))
    if args.filter_hrun is not None:
        fdict.append(filters.Filter_LocusHrun())
    if args.filter_regions is not None:
        filter_region_files = args.filter_regions.split(",")
        if args.filter_regions_names is not None:
            filter_region_names = args.filter_regions_names.split(",")
            if len(filter_region_names) != len(filter_region_files):
                common.ERROR("ERROR: length of --filter-regions-names must match --filter-regions\n")
        else: filter_region_names = [str(item) for item in list(range(len(filter_region_files)))]
        for i in range(len(filter_region_names)):
            fdict.append(filters.create_region_filter(filter_region_names[i], filter_region_files[i]))
    return fdict

def main():
    parser = argparse.ArgumentParser(__doc__)
    inout_group = parser.add_argument_group("Input/output")
    inout_group.add_argument("--vcf", help="Input STR VCF file", type=str, required=True)
    inout_group.add_argument("--out", help="Prefix for output files", type=str, required=True)

    call_group = parser.add_argument_group("Call-level filters")
    call_group.add_argument("--min-call-DP", help="Minimum call coverage", type=int)
    call_group.add_argument("--max-call-DP", help="Maximum call coverage", type=int)
    call_group.add_argument("--min-call-Q", help="Minimum call quality score", type=float)
    call_group.add_argument("--max-call-flank-indel", help="Maximum call flank indel rate", type=float)
    call_group.add_argument("--max-call-stutter", help="Maximum call stutter rate", type=float)

    locus_group = parser.add_argument_group("Locus-level filters")
    locus_group.add_argument("--min-locus-callrate", help="Minimum locus call rate", type=float)
    locus_group.add_argument("--min-locus-hwep", help="Filter loci failing HWE at this p-value threshold", type=float)
    locus_group.add_argument("--min-locus-het", help="Minimum locus heterozygosity", type=float)
    locus_group.add_argument("--max-locus-het", help="Maximum locus heterozygosity", type=float)
    locus_group.add_argument("--use-length", help="Calculate per-locus stats (het, HWE) collapsing alleles by length", action="store_true")
    locus_group.add_argument("--filter-regions", help="Comma-separated list of BED files of regions to filter", type=str)
    locus_group.add_argument("--filter-regions-names", help="Comma-separated list of filter names for each BED filter file", type=str)
    locus_group.add_argument("--filter-hrun", help="Filter STRs with long homopolymer runs.", action="store_true")
    locus_group.add_argument("--drop-filtered", help="Drop filtered records from output", action="store_true")

    debug_group = parser.add_argument_group("Debugging")
    debug_group.add_argument("--num-records", help="Only process this many records", type=int)

    args = parser.parse_args()

    # Load VCF file
    invcf = vcf.Reader(open(args.vcf, "rb"))

    # Set up filter list
    CheckFilters(invcf, args)
    invcf.filters = {}
    filter_list = BuildLocusFilters(args)
    for f in filter_list:
        short_doc = f.__doc__ or ''
        short_doc = short_doc.split('\n')[0].lstrip()
        invcf.filters[f.filter_name()] = _Filter(f.filter_name(), short_doc)
    call_filters = BuildCallFilters(args)

    # Add new FORMAT fields
    if "FILTER" not in invcf.formats:
        invcf.formats["FILTER"] = _Format("FILTER", 1, "String", "Call-level filter")

    # Add new INFO fields
    invcf.infos["AC"] = _Info("AC", -1, "Integer", "Alternate allele counts", source=None, version=None)
    invcf.infos["REFAC"] = _Info("REFAC", 1, "Integer", "Reference allele count", source=None, version=None)
    invcf.infos["HET"] = _Info("HET", 1, "Float", "Heterozygosity", source=None, version=None)
    invcf.infos["HWEP"] = _Info("HWEP", 1, "Float", "HWE p-value for obs. vs. exp het rate", source=None, version=None)
    invcf.infos["HRUN"] = _Info("HRUN", 1, "Integer", "Length of longest homopolymer run", source=None, version=None)

    # Set up output files
    outvcf = vcf.Writer(open(args.out + ".vcf", "w"), invcf)

    # Set up sample info
    all_reasons = GetAllCallFilters()
    sample_info = {}
    for s in invcf.samples:
        sample_info[s] = {"numcalls": 0, "totaldp": 0}
        for r in all_reasons: sample_info[s][r]  = 0

    # Set up locus info
    loc_info = {"totalcalls": 0, "PASS": 0} 
    for filt in filter_list: loc_info[filt.filter_name()] = 0

    # Go through each record
    record_counter = 0
    for record in invcf:
        record_counter += 1
        if args.num_records is not None and record_counter > args.num_records: break
        # Call-level filters
        record = ApplyCallFilters(record, invcf, call_filters, sample_info)

        # Locus-level filters
        record.FILTER = None
        output_record = True
        for filt in filter_list:
            if filt(record) == None: continue
            if args.drop_filtered:
                output_record = False
                break
            record.add_filter(filt.filter_name())
            loc_info[filt.filter_name()] += 1
        if output_record:
            # Recalculate locus-level INFO fields
            record.INFO["HRUN"] = utils.GetHomopolymerRun(record.REF)
            if record.num_called > 0:
                if args.use_length:
                    record.INFO["HET"] = utils.GetLengthHet(record)
                else: record.INFO["HET"] = record.heterozygosity
                record.INFO["HWEP"] = utils.GetSTRHWE(record, uselength=args.use_length)
                record.INFO["AC"] = [int(item*(2*record.num_called)) for item in record.aaf]
                record.INFO["REFAC"] = int((1-sum(record.aaf))*(2*record.num_called))
            else:
                record.INFO["HET"] = -1
                record.INFO["HWEP"] = -1
                record.INFO["AC"] = [0]*len(record.ALT)
                record.INFO["REFAC"] = 0
            # Recalc filter
            if record.FILTER is None and not args.drop_filtered:
                record.FILTER = "PASS"
                loc_info["PASS"] += 1
                loc_info["totalcalls"] += record.num_called
            # Output the record
            outvcf.write_record(record)

    # Output log info
    WriteSampLog(sample_info, all_reasons, args.out + ".samplog.tab")
    WriteLocLog(loc_info, args.out+".loclog.tab")

if __name__ == "__main__":
    main()