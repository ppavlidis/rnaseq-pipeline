import datetime
import logging
import os
from glob import glob
from os.path import join

import luigi
import luigi.task
import pandas as pd
import requests
import yaml
from bioluigi.scheduled_external_program import ScheduledExternalProgramTask
from bioluigi.tasks import fastqc, multiqc, cutadapt
from bioluigi.tasks.utils import DynamicTaskWithOutputMixin, TaskWithOutputMixin, DynamicWrapperTask
from luigi.task import flatten, flatten_output, WrapperTask
from luigi.util import requires

from .config import rnaseq_pipeline
from .sources.arrayexpress import DownloadArrayExpressSample, DownloadArrayExpressExperiment
from .sources.gemma import DownloadGemmaExperiment
from .sources.geo import DownloadGeoSample, DownloadGeoSeries, ExtractGeoSeriesBatchInfo
from .sources.local import DownloadLocalSample, DownloadLocalExperiment
from .sources.sra import DownloadSraProject, DownloadSraExperiment, ExtractSraProjectBatchInfo
from .targets import GemmaDatasetPlatform, GemmaDatasetFactor, RsemReference
from .utils import no_retry, GemmaTask, IlluminaFastqHeader, TaskWithPriorityMixin, RerunnableTaskMixin, remove_task_output

logger = logging.getLogger('luigi-interface')

cfg = rnaseq_pipeline()

class DownloadSample(TaskWithOutputMixin, WrapperTask):
    """
    This is a generic task for downloading an individual sample in an
    experiment.

    Note that the 'gemma' source does not provide individual samples.
    """
    experiment_id = luigi.Parameter()
    sample_id = luigi.Parameter()

    source = luigi.ChoiceParameter(default='local', choices=['gemma', 'geo', 'arrayexpress', 'local', 'sra'], positional=False)

    def requires(self):
        if self.source in ['geo', 'gemma']:
            return DownloadGeoSample(self.sample_id)
        elif self.source == 'sra':
            return DownloadSraExperiment(self.sample_id)
        elif self.source == 'arrayexpress':
            return DownloadArrayExpressSample(self.experiment_id, self.sample_id)
        elif self.source == 'local':
            return DownloadLocalSample(self.experiment_id, self.sample_id)
        else:
            raise ValueError('Unknown source for sample: {}.'.format(self.source))

class DownloadExperiment(TaskWithPriorityMixin, TaskWithOutputMixin, WrapperTask):
    """
    This is a generic task that detects which kind of experiment is intended to
    be downloaded so that downstream tasks can process regardless of the data
    source.

    :source: Indicate the origin of the experiment, otherwise it will be
    inferred from the :experiment_id: parameter.
    """
    experiment_id = luigi.Parameter()

    source = luigi.ChoiceParameter(default='local', choices=['gemma', 'geo', 'sra', 'arrayexpress', 'local'], positional=False)

    def requires(self):
        if self.source == 'gemma':
            return DownloadGemmaExperiment(self.experiment_id)
        elif self.source == 'geo':
            return DownloadGeoSeries(self.experiment_id)
        elif self.source == 'sra':
            return DownloadSraProject(self.experiment_id)
        elif self.source == 'arrayexpress':
            return DownloadArrayExpressExperiment(self.experiment_id)
        elif self.source == 'local':
            return DownloadLocalExperiment(self.experiment_id)
        else:
            raise ValueError('Unknown download source for experiment: {}.')

@requires(DownloadSample)
class TrimSample(DynamicTaskWithOutputMixin, DynamicWrapperTask):
    """
    Trim Illumina Universal Adapter from single end and paired reads.
    """

    def run(self):
        destdir = join(cfg.OUTPUT_DIR, 'data-trimmed', self.experiment_id, self.sample_id)
        os.makedirs(destdir, exist_ok=True)
        download_sample_task = self.requires()
        platform = download_sample_task.requires().platform
        if len(self.input()) == 1:
            r1, = self.input()
            yield platform.get_trim_single_end_reads_task(
                    r1.path,
                    join(destdir, os.path.basename(r1.path)),
                    minimum_length=25,
                    cpus=4)
        elif len(self.input()) == 2:
            r1, r2 = self.input()
            yield platform.get_trim_paired_reads_task(
                    r1.path, r2.path,
                    join(destdir, os.path.basename(r1.path)),
                    join(destdir, os.path.basename(r2.path)),
                    minimum_length=25,
                    cpus=4)
        else:
            raise NotImplementedError('Trimming more than two mates is not supported.')

class TrimExperiment(TaskWithPriorityMixin, DynamicTaskWithOutputMixin, DynamicWrapperTask):
    """
    Quality control all the samples in a given experiment.
    """
    experiment_id = luigi.Parameter()
    source = luigi.ChoiceParameter(default='local', choices=['gemma', 'geo', 'sra', 'arrayexpress', 'local'], positional=False)

    def requires(self):
        return DownloadExperiment(self.experiment_id, source=self.source).requires().requires()

    def run(self):
        download_sample_tasks = next(DownloadExperiment(self.experiment_id, source=self.source).requires().run())
        yield [TrimSample(self.experiment_id, dst.sample_id, source=self.source)
               for dst in download_sample_tasks]

@no_retry
@requires(TrimSample)
class QualityControlSample(DynamicTaskWithOutputMixin, DynamicWrapperTask):
    """
    Perform post-download quality control on the FASTQs.
    """
    def run(self):
        destdir = join(cfg.OUTPUT_DIR, cfg.DATAQCDIR, self.experiment_id, self.sample_id)
        os.makedirs(destdir, exist_ok=True)
        yield [fastqc.GenerateReport(fastq_in.path, destdir) for fastq_in in self.input()]

class QualityControlExperiment(TaskWithPriorityMixin, DynamicTaskWithOutputMixin, DynamicWrapperTask):
    """
    Quality control all the samples in a given experiment.
    """
    experiment_id = luigi.Parameter()
    source = luigi.ChoiceParameter(default='local', choices=['gemma', 'geo', 'sra', 'arrayexpress', 'local'], positional=False)

    def requires(self):
        return DownloadExperiment(self.experiment_id, source=self.source).requires().requires()

    def run(self):
        download_sample_tasks = next(DownloadExperiment(self.experiment_id, source=self.source).requires().run())
        yield [QualityControlSample(self.experiment_id,
                                    dst.sample_id,
                                    source=self.source)
               for dst in download_sample_tasks]

@no_retry
class PrepareReference(ScheduledExternalProgramTask):
    """
    Prepare a STAR/RSEM reference.

    :param taxon: Taxon
    :param reference_id: Reference annotation build to use (i.e. ensembl98, hg38_ncbi)
    """
    taxon = luigi.Parameter(default='human')
    reference_id = luigi.Parameter(default='hg38_ncbi')

    cpus = 16
    memory = 32

    def input(self):
        genome_dir = join(cfg.OUTPUT_DIR, cfg.GENOMES, self.reference_id)
        gtf_files = glob(join(genome_dir, '*.gtf'))
        fasta_files = glob(join(genome_dir, '*.fa'))
        if len(gtf_files) != 1:
            raise ValueError('Only one GTF file is expected in {}.'.format(genome_dir))
        return [luigi.LocalTarget(gtf_files[0]),
                [luigi.LocalTarget(f) for f in fasta_files]]

    def program_args(self):
        gtf, genome_fasta = self.input()
        args = [join(cfg.RSEM_DIR, 'rsem-prepare-reference')]

        args.extend(['--gtf', gtf.path])

        args.extend([
            '--star',
            '-p', self.cpus])

        args.extend([t.path for t in genome_fasta])

        args.append(join(self.output().prefix))

        return args

    def run(self):
        os.makedirs(os.path.dirname(self.output().prefix), exist_ok=True)
        return super().run()

    def output(self):
        return RsemReference(join(cfg.OUTPUT_DIR, cfg.REFERENCES, self.reference_id), self.taxon)

@no_retry
@requires(TrimSample, PrepareReference)
class AlignSample(ScheduledExternalProgramTask):
    """
    The output of the task is a pair of isoform and gene quantification results
    processed by STAR and RSEM.

    :attr ignore_mate: Ignore the second mate in the case of paired reads
    :attr strand_specific: Indicate if the RNA-Seq data is stranded
    """
    ignore_mate = luigi.BoolParameter(default=False, positional=False)

    # TODO: handle strand-specific reads
    strand_specific = luigi.BoolParameter(default=False, positional=False)

    scope = luigi.Parameter(default='genes', positional=False)

    cpus = 8
    memory = 32

    # cleanup unused shared memory objects before and after the task is run
    scheduler_extra_args = ["--task-prolog", join(os.path.dirname(__file__), "../scripts/clean-unused-shm-objects"),
                            "--task-epilog", join(os.path.dirname(__file__), "../scripts/clean-unused-shm-objects")]

    def run(self):
        self.output().makedirs()
        return super().run()

    def _get_output_prefix(self):
        return join(cfg.OUTPUT_DIR, cfg.ALIGNDIR, self.reference_id, self.experiment_id, self.sample_id)

    def program_args(self):
        args = [join(cfg.RSEM_DIR, 'rsem-calculate-expression'), '-p', self.cpus]

        args.extend([
            '--time',
            '--star',
            '--star-gzipped-read-file',
            '--no-bam-output'])

        if self.strand_specific:
            args.append('--strand-specific')

        sample_run, reference = self.input()

        fastqs = [mate.path for mate in sample_run]

        if len(fastqs) == 1:
            args.append(fastqs[0])
        elif len(fastqs) == 2 and self.ignore_mate:
            logger.info('Mate is ignored for %s.', repr(self))
            args.append(fastqs[0])
        elif len(fastqs) == 2:
            args.append('--paired-end')
            args.extend(fastqs)
        else:
            raise NotImplementedError('Alignment of more than two input FASTQs is not supported.')

        # reference for alignments and quantifications
        args.append(join(reference.prefix, '{}_0'.format(self.taxon)))

        # output prefix
        args.append(self._get_output_prefix())

        return args

    def output(self):
        return luigi.LocalTarget(self._get_output_prefix() + f'.{self.scope}.results')

class AlignExperiment(TaskWithPriorityMixin, DynamicTaskWithOutputMixin, DynamicWrapperTask):
    """
    Align all the samples in a given experiment.

    The output is one sample alignment output per sample contained in the
    experiment.
    """
    experiment_id = luigi.Parameter()
    source = luigi.ChoiceParameter(default='local', choices=['gemma', 'geo', 'sra', 'arrayexpress', 'local'], positional=False)
    taxon = luigi.Parameter(default='human', positional=False)
    reference_id = luigi.Parameter(default='hg38_ncbi', positional=False)
    scope = luigi.Parameter(default='genes', positional=False)

    def requires(self):
        return DownloadExperiment(self.experiment_id, source=self.source).requires().requires()

    def run(self):
        download_sample_tasks = next(DownloadExperiment(self.experiment_id, source=self.source).requires().run())
        yield [AlignSample(self.experiment_id,
                           dst.sample_id,
                           source=self.source,
                           taxon=self.taxon,
                           reference_id=self.reference_id,
                           scope=self.scope)
               for dst in download_sample_tasks]

@no_retry
@requires(TrimExperiment, QualityControlExperiment, AlignExperiment)
class GenerateReportForExperiment(TaskWithPriorityMixin, luigi.Task):
    """
    Generate a summary report for an experiment with MultiQC.

    The report include collected FastQC reports and RSEM/STAR outputs.
    """

    def run(self):
        search_dirs = [
            join(cfg.OUTPUT_DIR, cfg.DATAQCDIR, self.experiment_id),
            join(cfg.OUTPUT_DIR, cfg.ALIGNDIR, self.reference_id, self.experiment_id)]
        self.output().makedirs()
        yield multiqc.GenerateReport(search_dirs, os.path.dirname(self.output().path))

    def output(self):
        return luigi.LocalTarget(join(cfg.OUTPUT_DIR, 'report', self.reference_id, self.experiment_id, 'multiqc_report.html'))

@requires(AlignExperiment)
class CountExperiment(TaskWithPriorityMixin, luigi.Task):
    """
    Combine the RSEM quantifications results from all the samples in a given
    experiment.

    The output is constituted of two matrices: genes counts and FPKM.
    """
    resources = {'cpus': 1}

    def run(self):
        # FIXME: find a better way to obtain the sample identifier
        # Each DownloadSample-like tasks have a sample_id property! Use that!
        keys = [os.path.basename(f.path).replace(f'.{self.scope}.results', '') for f in self.input()]
        counts_df = pd.concat([pd.read_csv(f.path, sep='\t', index_col=0).expected_count for f in self.input()], keys=keys, axis=1)
        fpkm_df = pd.concat([pd.read_csv(f.path, sep='\t', index_col=0).FPKM for f in self.input()], keys=keys, axis=1)

        with self.output()[0].open('w') as counts_out, self.output()[1].open('w') as fpkm_out:
            counts_df.to_csv(counts_out, sep='\t')
            fpkm_df.to_csv(fpkm_out, sep='\t')

    def output(self):
        destdir = join(cfg.OUTPUT_DIR, cfg.QUANTDIR, self.reference_id)
        return [luigi.LocalTarget(join(destdir, f'{self.experiment_id}_counts.{self.scope}')),
                luigi.LocalTarget(join(destdir, f'{self.experiment_id}_fpkm.{self.scope}'))]

class SubmitExperimentBatchInfoToGemma(TaskWithPriorityMixin, GemmaTask):
    """
    Submit the batch information of an experiment to Gemma.
    """

    subcommand = 'fillBatchInfo'

    resources = {'submit_batch_info_jobs': 1}

    def is_batch_info_usable(self):
        batch_info_df = pd.read_csv(self.input().path, sep='\t', names=['sample_id', 'run_id', 'platform_id', 'srx_url', 'fastq_header'])
        batch = set()
        for ix, row in batch_info_df.iterrows():
            try:
                _, fastq_header, _ = row.fastq_header.split()
                illumina_header = IlluminaFastqHeader.parse(fastq_header)
                batch.add((row.platform_id,) + illumina_header.batch_factor)
            except TypeError:
                logger.debug('%s does not have Illumina-formatted FASTQ headers: %s', row.run_id, fastq_header)
                batch.add(None)
        return len(batch) > 1

    def requires(self):
        # TODO: Have a generic strategy for extracting batch info that would
        # work for all sources
        if self.external_database == 'GEO':
            return ExtractGeoSeriesBatchInfo(self.accession)
        elif self.external_database == 'SRA':
            return ExtractSraProjectBatchInfo(self.accession)
        else:
            raise NotImplementedError('Extracting batch information from {} is not implemented.'.format(self.external_database))

    def subcommand_args(self):
        return ['--force', 'yes', '-f', self.input().path]

    def run(self):
        if self.is_batch_info_usable():
            return super().run()
        else:
            logger.info('Batch info is unusable for %s.', self.experiment_id)

    def output(self):
        return GemmaDatasetFactor(self.experiment_id, 'batch')

    def complete(self):
        if all(req.complete() for req in flatten(self.requires())):
            logger.info('Batch info is unusable for %s.', self.experiment_id)
            return not self.is_batch_info_usable() or super().complete()
        else:
            return super().complete()

@no_retry
class SubmitExperimentDataToGemma(TaskWithPriorityMixin, RerunnableTaskMixin, GemmaTask):
    """
    Submit an experiment to Gemma.

    The metadata containing the taxon are fetched from Gemma REST API.

    This will trigger the whole pipeline.

    The output is a confirmation that the data is effectively in Gemma.
    """

    subcommand = 'rnaseqDataAdd'

    resources = {'submit_data_jobs': 1}

    def requires(self):
        yield CountExperiment(self.experiment_id,
                              taxon=self.taxon,
                              reference_id=self.reference_id,
                              source='gemma',
                              scope='genes')

        # FIXME: this does not always trigger the dependencies
        yield GenerateReportForExperiment(self.experiment_id,
                                          taxon=self.taxon,
                                          reference_id=self.reference_id,
                                          source='gemma')

    def subcommand_args(self):
        (count, fpkm), _ = self.input()
        # TODO: submit the batch information and the MultiQC report
        return ['-a', self.platform_short_name,
                '-count', count.path,
                '-rpkm', fpkm.path]

    def output(self):
        return GemmaDatasetPlatform(self.experiment_id, self.platform_short_name)

@requires(SubmitExperimentDataToGemma, SubmitExperimentBatchInfoToGemma)
class SubmitExperimentToGemma(TaskWithPriorityMixin, TaskWithOutputMixin, WrapperTask):
    """
    Submit an experiment data, QC reports, and batch information to Gemma.

    TODO: add QC report submission
    """

    def _targets_to_remove(self):
        outs = []
        # original data
        # TODO: check if data is local, we don't want to delete that
        download_task = DownloadExperiment(self.experiment_id, source='gemma')
        outs.extend(flatten_output(download_task))
        # any data resulting from trimming raw reads
        trim_task = TrimExperiment(self.experiment_id, source='gemma')
        outs.extend(flatten_output(trim_task))
        return outs

    def on_success(self):
        # report success to curators
        if cfg.SLACK_WEBHOOK_URL is not None:
            payload = {'text': '<https://gemma.msl.ubc.ca/expressionExperiment/showExpressionExperiment.html?shortName={0}|{0}> data and batch information have been successfully submitted to Gemma.'.format(self.experiment_id)}
            requests.post(cfg.SLACK_WEBHOOK_URL, json=payload)
        return super().on_success()

    def run(self):
        # cleanup download data
        remove_task_output(DownloadExperiment(self.experiment_id, source='gemma'))
        remove_task_output(TrimExperiment(self.experiment_id, source='gemma'))

    def complete(self):
        """
        We consider a submission completed when data and batch information have
        been successfully submitted to Gemma and any remaining downloaded and
        trimmed data has been removed.

        Ideally we would delete the data earlier, but it's necessary for both
        quantification and batch info extraction.
        """
        return super().complete() and all(not out.exists() for out in self._targets_to_remove())

class SubmitExperimentsFromFileToGemma(TaskWithOutputMixin, WrapperTask):
    input_file = luigi.Parameter()
    def requires(self):
        df = pd.read_csv(self.input_file, sep='\t', converters={'priority': lambda x: 0 if x == '' else int(x)})
        return [SubmitExperimentToGemma(row.experiment_id, priority=row.get('priority', 0), rerun=row.get('data')=='resubmit')
                for _, row in df.iterrows() if row.get('priority', 0) > 0]

class SubmitExperimentsFromGoogleSpreadsheetToGemma(luigi.Task):
    spreadsheet_id = luigi.Parameter()
    sheet_name = luigi.Parameter()
    def requires(self):
        from .gsheet import retrieve_spreadsheet
        df = retrieve_spreadsheet(self.spreadsheet_id, self.sheet_name)
        return [SubmitExperimentToGemma(row.experiment_id, priority=row.get('priority', 0), rerun=row['data']=='resubmit')
                for _, row in df.iterrows() if row.get('priority', 0) > 0]
