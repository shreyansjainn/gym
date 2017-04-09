#!/usr/bin/env python3
import argparse
import io
import logging
import os
import subprocess
from glob import glob

import collections

import gym
import matplotlib.pyplot as plt
import numpy as np
import time

import yaml
from flask import Flask
from flask import render_template
from gym import monitoring
from gym.benchmarks import registry
from gym.benchmarks.viewer.error import Error
from gym.benchmarks.viewer.template_helpers import register_template_helpers
from gym.benchmarks.viewer.utils import time_elapsed
from scipy import signal

import sys
# Disable spurious warning https://github.com/scipy/scipy/issues/5998
import warnings

warnings.filterwarnings(action="ignore", module="scipy", message="^internal gelsd")

logger = logging.getLogger(__name__)

#############################
# Args
#############################

parser = argparse.ArgumentParser()
parser.add_argument('data_path',
    help="The path to our benchmark data. e.g. /tmp/Atari40M/ ")
parser.add_argument('--port', default=5000, type=int, help="Open the browser")
parser.add_argument('--debug', action="store_true",
    help="Run with debugger and auto-reload")
parser.add_argument('--flush-cache', action="store_true",
    help="NOT YET IMPLEMENTED: Flush the cache and recompute ranks from scratch")
parser.add_argument('--watch', action="store_true",
    help="NOT YET IMPLEMENTED: Watch for changes and refresh")
parser.add_argument('--open', action="store_true",
    help="Open the browser")
ARGS = parser.parse_args()

BENCHMARK_VIEWER_DATA_PATH = ARGS.data_path.rstrip('/')
BMRUNS_DIR = os.path.join(BENCHMARK_VIEWER_DATA_PATH, 'runs')

yaml_file = os.path.join(BENCHMARK_VIEWER_DATA_PATH, 'benchmark_metadata.yaml')

with open(yaml_file, 'r') as stream:
    try:
        metadata = yaml.load(stream)
        BENCHMARK_ID = metadata['id']

    except yaml.YAMLError as exc:
        print(exc)
        sys.exit()

BENCHMARK_SPEC = gym.benchmark_spec(BENCHMARK_ID)

app = Flask(__name__)


#############################
# Cache
#############################

class ScoreCache(object):
    """
    Stores data about the benchmark in memory
    """

    def __init__(self, benchmark_id):
        self._env_id_to_min_scoring_bmrun = {}
        self._env_id_to_max_scoring_bmrun = {}

        # Maps evaluation_dir to score, and date added
        self._evaluation_cache = {}

        self.id = benchmark_id

    def cache_evaluation_score(self, bmrun, evaluation, score):
        env_id = evaluation.env_id

        # See if we should overwrite the min or max
        if self.min_score(env_id) is None or score < self.min_score(env_id):
            self._env_id_to_min_scoring_bmrun[env_id] = {
                'bmrun_name': bmrun.name,
                'score': score
            }

        if self.max_score(env_id) is None or score > self.max_score(env_id):
            self._env_id_to_max_scoring_bmrun[env_id] = {
                'bmrun_name': bmrun.name,
                'score': score
            }

        # Cache the time also, so we know when to cachebust
        self._evaluation_cache[evaluation] = {
            'score': score,
            'score_cached_at': time.time()
        }

    def min_score(self, env_id):
        """The worst evaluation performance we've seen on this env on this benchmark"""
        try:
            return self._env_id_to_min_scoring_bmrun[env_id]['score']
        except KeyError:
            return None

    def max_score(self, env_id):
        """The best evaluation performance we've seen on this env on this benchmark"""
        try:
            return self._env_id_to_max_scoring_bmrun[env_id]['score']
        except KeyError:
            return None

    @property
    def spec(self):
        return gym.benchmark_spec(self.id)


score_cache = ScoreCache(BENCHMARK_ID)


#############################
# Resources
#############################

class BenchmarkResource(object):
    def __init__(self, id, data_path, bmruns):
        self.id = id
        self.data_path = data_path
        self.bmruns = bmruns

    @property
    def spec(self):
        return gym.benchmark_spec(self.id)


Evaluation = collections.namedtuple('Evaluation', [
    'score',
    'env_id',
    'episode_rewards',
    'episode_lengths',
    'episode_types',
    'timestamps',
    'initial_reset_timestamps',
    'data_sources',
])


def load_evaluation(training_dir):
    results = monitoring.load_results(training_dir)
    env_id = results['env_info']['env_id']

    score = compute_evaluation_score(env_id, results)
    return Evaluation(**{
        'score': score,
        'env_id': env_id,
        'episode_rewards': results['episode_rewards'],
        'episode_lengths': results['episode_lengths'],
        'episode_types': results['episode_types'],
        'timestamps': results['timestamps'],
        'initial_reset_timestamps': results['initial_reset_timestamps'],
        'data_sources': results['data_sources']
    })


def compute_evaluation_score(env_id, results):
    benchmark = registry.benchmark_spec(BENCHMARK_ID)

    score_results = benchmark.score_evaluation(
        env_id,
        data_sources=results['data_sources'],
        initial_reset_timestamps=results['initial_reset_timestamps'],
        episode_lengths=results['episode_lengths'],
        episode_rewards=results['episode_rewards'],
        episode_types=results['episode_types'],
        timestamps=results['timestamps']
    )

    # TODO: Why does the scorer output vectorized here?
    return mean_area_under_curve(
        score_results['lengths'][0],
        score_results['rewards'][0],
    )


class BenchmarkRunResource(object):
    def __init__(self,
            path,
            task_runs,
            username,
            title,
            repository,
            commit,
            command,
    ):
        self.task_runs = task_runs
        self.name = os.path.basename(path)

        self.path = path
        self.username = username
        self.title = title
        self.repository = repository
        self.commit = commit
        self.command = command

    @property
    def evaluations(self):
        return [evaluation for task in self.task_runs for evaluation in task.evaluations]


# TaskResource = NamedTuple('TaskResource', ('benchmark_id', 'evaluations'))

class TaskRunResource(object):
    def __init__(self, env_id, benchmark_id, evaluations):
        self.env_id = env_id
        self.benchmark_id = benchmark_id
        self.evaluations = evaluations

    @property
    def spec(self):
        benchmark_spec = registry.benchmark_spec(self.benchmark_id)
        task_specs = benchmark_spec.task_specs(self.env_id)
        if len(task_specs) != 1:
            raise Error("Multiple task specs for single environment. Falling over")

        return task_specs[0]

    def render_learning_curve_svg(self):
        return render_evaluation_learning_curves_svg(self.evaluations, self.spec.max_timesteps)

    def render_learning_curve_svg_comparison(self, other_bmrun):
        # TODO: Move this out of the resource
        other_task = [task for task in other_bmrun if task.env_id == self.env_id][0]
        evaluations = self.evaluations + other_task.evaluations
        return render_evaluation_learning_curves_svg(evaluations, self.spec.max_timesteps)


#############################
# Graph rendering
#############################

def area_under_curve(episode_lengths, episode_rewards):
    """Compute the total area of rewards under the curve"""
    # TODO: Replace with slightly more accurate trapezoid method
    return np.sum(l * r for l, r in zip(episode_lengths, episode_rewards))


def mean_area_under_curve(episode_lengths, episode_rewards):
    """Compute the average area of rewards under the curve per unit of time"""
    return area_under_curve(episode_lengths, episode_rewards) / max(1e-4, np.sum(episode_lengths))


def smooth_reward_curve(rewards, lengths, max_timestep, resolution=1e3, polyorder=3):
    # Don't use a higher resolution than the original data, use a window about
    # 1/10th the resolution
    resolution = min(len(rewards), resolution)
    window = int(resolution / 10)
    window = window + 1 if (window % 2 == 0) else window  # window must be odd

    if polyorder >= window:
        return lengths, rewards

    # Linear interpolation, followed by Savitzky-Golay filter
    x = np.cumsum(np.array(lengths, 'float'))
    y = np.array(rewards, 'float')

    x_spaced = np.linspace(0, max_timestep, resolution)
    y_interpolated = np.interp(x_spaced, x, y)
    y_smoothed = signal.savgol_filter(y_interpolated, window, polyorder=polyorder)

    return x_spaced.tolist(), y_smoothed.tolist()


def render_evaluation_learning_curves_svg(evaluations, max_timesteps):
    plt.rcParams['figure.figsize'] = (8, 2)
    plt.figure()
    for evaluation in evaluations:
        xs, ys = smooth_reward_curve(
            evaluation.episode_rewards, evaluation.episode_lengths, max_timesteps)
        plt.plot(xs, ys)

    plt.xlabel('Time')
    plt.ylabel('Rewards')
    plt.tight_layout()
    img_bytes = io.StringIO()
    plt.savefig(img_bytes, format='svg')
    plt.close()
    return img_bytes.getvalue()


#############################
# Controllers
#############################

@app.route('/')
def index():
    benchmark = BenchmarkResource(
        id=BENCHMARK_ID,
        data_path=BMRUNS_DIR,
        bmruns=_benchmark_runs_from_dir(BMRUNS_DIR)
    )

    return render_template('benchmark.html',
        benchmark=benchmark,
        score_cache=score_cache
    )


@app.route('/compare/<bmrun_name>/<other_bmrun_name>/')
def compare(bmrun_name, other_bmrun_name):
    bmrun_dir = os.path.join(BMRUNS_DIR, bmrun_name)
    bmrun = load_bmrun_from_path(bmrun_dir)

    other_bmrun_dir = os.path.join(BMRUNS_DIR, other_bmrun_name)
    other_bmrun = load_bmrun_from_path(other_bmrun_dir)

    return render_template('compare.html',
        bmrun=bmrun,
        other_bmrun=other_bmrun,
        benchmark_spec=BENCHMARK_SPEC,
        score_cache=score_cache
    )


@app.route('/benchmark_run/<bmrun_name>')
def benchmark_run(bmrun_name):
    bmrun_dir = os.path.join(BMRUNS_DIR, bmrun_name)
    bmrun = load_bmrun_from_path(bmrun_dir)

    return render_template('benchmark_run.html',
        bmrun=bmrun,
        benchmark_spec=BENCHMARK_SPEC,
        score_cache=score_cache
    )


#############################
# Data loading
#############################


def load_evaluations_from_bmrun_path(path):
    evaluations = []

    dirs_missing_manifests = []
    dirs_unloadable = []

    for training_dir in glob('%s/*/gym' % path):

        if not monitoring.detect_training_manifests(training_dir):
            dirs_missing_manifests.append(training_dir)
            continue

        evaluation = load_evaluation(training_dir)
        evaluations.append(evaluation)

    if dirs_missing_manifests:
        logger.warning("Could not load %s evaluations in %s due to missing manifests" % (
            len(dirs_missing_manifests), path))

    if dirs_unloadable:
        logger.warning(
            "monitoring.load_results failed on %s evaluations in %s" % (len(dirs_unloadable), path))

    return evaluations


def load_task_runs_from_bmrun_path(path, benchmark_spec):
    """
    Returns a list of task_runs sorted alphabetically by env_id
    """
    env_id_to_task_run = {}
    for task in benchmark_spec.tasks:
        env_id_to_task_run[task.env_id] = TaskRunResource(
            task.env_id,
            benchmark_id=BENCHMARK_ID,
            evaluations=[])

    for evaluation in load_evaluations_from_bmrun_path(path):
        env_id = evaluation.env_id
        env_id_to_task_run[env_id].evaluations.append(evaluation)

    task_runs = env_id_to_task_run.values()
    return sorted(task_runs, key=lambda t: t.env_id)


def load_bmrun_from_path(path):
    task_runs = load_task_runs_from_bmrun_path(path, BENCHMARK_SPEC)

    # Load in metadata from yaml
    metadata = None
    metadata_file = os.path.join(path, 'benchmark_run_metadata.yaml')

    with open(metadata_file, 'r') as stream:
        try:
            metadata = yaml.load(stream)
        except yaml.YAMLError as exc:
            raise exc
    try:
        return BenchmarkRunResource(path,
            task_runs,
            username=metadata['username'],
            title=metadata['title'],
            repository=metadata['repository'],
            commit=metadata['commit'],
            command=metadata['command'],
        )
    except KeyError as e:
        logger.error("Missing key %s in metadata file: %s" % (str(e), metadata_file))
        raise e


def _benchmark_runs_from_dir(benchmark_dir):
    run_paths = [os.path.join(benchmark_dir, path) for path in os.listdir(benchmark_dir)]
    run_paths = [path for path in run_paths if os.path.isdir(path)]
    return [load_bmrun_from_path(path) for path in run_paths]


def populate_benchmark_cache():
    logger.info("Loading in all benchmark_runs from %s..." % BMRUNS_DIR)
    time_elapsed()
    bmruns = _benchmark_runs_from_dir(BMRUNS_DIR)
    logger.info("Loaded %s benchmark_runs in %s seconds." % (len(bmruns), time_elapsed()))

    logger.info("Computing scores for each task...")
    for bmrun in bmruns:
        for task_run in bmrun.task_runs:
            for evaluation in task_run.evaluations:
                score_cache.cache_evaluation_score(bmrun, task_run, evaluation.score)

        logger.info("Computed scores for %s" % bmrun.name)

    logger.info("Computed scores for %s benchmark_runs in %s seconds." %
                (len(bmruns), time_elapsed()))


if __name__ == '__main__':
    logger.setLevel(logging.INFO)
    populate_benchmark_cache()
    logger.info("All data loaded, Viewer is ready to go at http://localhost:5000")

    if ARGS.open:
        subprocess.check_call('open http://localhost:%s' % ARGS.port, shell=True)

    register_template_helpers(app)
    app.run(debug=ARGS.debug, port=ARGS.port)