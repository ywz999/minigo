# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Runs a reinforcement learning loop to train a Go playing model."""

import sys
sys.path.insert(0, '.')  # nopep8

import asyncio
import logging
import numpy as np
import os
import random
import re
import shutil
import subprocess
import tensorflow as tf
import time
import utils

from absl import app, flags
from rl_loop import example_buffer, fsdb
from tensorflow import gfile

flags.DEFINE_integer('iterations', 100, 'Number of iterations of the RL loop.')

flags.DEFINE_float('gating_win_rate', 0.55,
                   'Win-rate against the current best required to promote a '
                   'model to new best.')

flags.DEFINE_string('flags_dir', None,
                    'Directory in which to find the flag files for each stage '
                    'of the RL loop. The directory must contain the following '
                    'files: bootstrap.flags, selfplay.flags, eval.flags, '
                    'train.flags.')

flags.DEFINE_integer('max_window_size', 5,
                     'Maximum number of recent selfplay rounds to train on.')

flags.DEFINE_integer('slow_window_size', 5,
                     'Window size after which the window starts growing by '
                     '1 every slow_window_speed iterations of the RL loop.')

flags.DEFINE_integer('slow_window_speed', 1,
                     'Speed at which the training window increases in size '
                     'once the window size passes slow_window_size.')

flags.DEFINE_boolean('parallel_post_train', False,
                     'If true, run the post-training stages (eval, validation '
                     '& selfplay) in parallel.')

flags.DEFINE_string('engine', 'tf', 'The engine to use for selfplay.')

FLAGS = flags.FLAGS


class State:
  """State data used in each iteration of the RL loop.

  Models are named with the current reinforcement learning loop iteration number
  and the model generation (how many models have passed gating). For example, a
  model named "000015-000007" was trained on the 15th iteration of the loop and
  is the 7th models that passed gating.
  Note that we rely on the iteration number being the first part of the model
  name so that the training chunks sort correctly.
  """

  def __init__(self):
    self.start_time = time.time()

    self.iter_num = 0
    self.gen_num = 0

    self.best_model_name = None

  @property
  def output_model_name(self):
    return '%06d-%06d' % (self.iter_num, self.gen_num)

  @property
  def train_model_name(self):
    return '%06d-%06d' % (self.iter_num, self.gen_num + 1)

  @property
  def best_model_path(self):
    if self.best_model_name is None:
      # We don't have a good model yet, use a random fake model implementation.
      return 'random:0,0.4:0.4'
    else:
      return '{},{}.pb'.format(
         FLAGS.engine, os.path.join(fsdb.models_dir(), self.best_model_name))

  @property
  def train_model_path(self):
    return '{},{}.pb'.format(
         FLAGS.engine, os.path.join(fsdb.models_dir(), self.train_model_name))

  @property
  def seed(self):
    return self.iter_num + 1


class ColorWinStats:
  """Win-rate stats for a single model & color."""

  def __init__(self, total, both_passed, opponent_resigned, move_limit_reached):
    self.total = total
    self.both_passed = both_passed
    self.opponent_resigned = opponent_resigned
    self.move_limit_reached = move_limit_reached
    # Verify that the total is correct
    assert total == both_passed + opponent_resigned + move_limit_reached


class WinStats:
  """Win-rate stats for a single model."""

  def __init__(self, line):
    pattern = '\s*(\S+)' + '\s+(\d+)' * 8
    match = re.search(pattern, line)
    if match is None:
        raise ValueError('Can\t parse line "{}"'.format(line))
    self.model_name = match.group(1)
    raw_stats = [float(x) for x in match.groups()[1:]]
    self.black_wins = ColorWinStats(*raw_stats[:4])
    self.white_wins = ColorWinStats(*raw_stats[4:])
    self.total_wins = self.black_wins.total + self.white_wins.total


def parse_win_stats_table(stats_str, num_lines):
  result = []
  lines = stats_str.split('\n')
  while True:
    # Find the start of the win stats table.
    assert len(lines) > 1
    if 'Black' in lines[0] and 'White' in lines[0] and 'm.lmt.' in lines[1]:
        break
    lines = lines[1:]

  # Parse the expected number of lines from the table.
  for line in lines[2:2 + num_lines]:
    result.append(WinStats(line))

  return result


def expand_cmd_str(cmd):
  return '  '.join(flags.FlagValues().read_flags_from_files(cmd))


def get_cmd_name(cmd):
  if cmd[0] == 'python' or cmd[0] == 'python3':
    path = cmd[1]
  else:
    path = cmd[0]
  return os.path.splitext(os.path.basename(path))[0]


async def checked_run(*cmd):
  """Run the given subprocess command in a coroutine.

  Args:
    *cmd: the command to run and its arguments.

  Returns:
    The output that the command wrote to stdout as a list of strings, one line
    per element (stderr output is piped to stdout).

  Raises:
    RuntimeError: if the command returns a non-zero result.
  """

  # Start the subprocess.
  logging.info('Running: %s', expand_cmd_str(cmd))
  with utils.logged_timer('{} finished'.format(get_cmd_name(cmd))):
    p = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

    # Stream output from the process stdout.
    chunks = []
    while True:
      chunk = await p.stdout.read(16 * 1024)
      if not chunk:
        break
      chunks.append(chunk)

    # Wait for the process to finish, check it was successful & build stdout.
    await p.wait()
    stdout = b''.join(chunks).decode()[:-1]
    if p.returncode:
      raise RuntimeError('Return code {} from process: {}\n{}'.format(
          p.returncode, expand_cmd_str(cmd), stdout))

    log_path = os.path.join(FLAGS.log_dir, get_cmd_name(cmd) + '.log')
    with gfile.Open(log_path, 'a') as f:
      f.write(expand_cmd_str(cmd))
      f.write('\n')
      f.write(stdout)
      f.write('\n')

    # Split stdout into lines.
    return stdout.split('\n')


def wait(aws):
  """Waits for all of the awaitable objects (e.g. coroutines) in aws to finish.

  All the awaitable objects are waited for, even if one of them raises an
  exception. When one or more awaitable raises an exception, the exception from
  the awaitable with the lowest index in the aws list will be reraised.

  Args:
    aws: a single awaitable, or list awaitables.

  Returns:
    If aws is a single awaitable, its result.
    If aws is a list of awaitables, a list containing the of each awaitable in
    the list.

  Raises:
    Exception: if any of the awaitables raises.
  """

  aws_list = aws if isinstance(aws, list) else [aws]
  results = asyncio.get_event_loop().run_until_complete(asyncio.gather(
      *aws_list, return_exceptions=True))
  # If any of the cmds failed, re-raise the error.
  for result in results:
    if isinstance(result, Exception):
      raise result
  return results if isinstance(aws, list) else results[0]


def get_golden_chunk_records(num_records):
  """Return up to num_records of golden chunks to train on.

  Args:
    num_records: maximum number of records to return.

  Returns:
    A list of golden chunks up to num_records in length, sorted by path.
  """

  pattern = os.path.join(fsdb.golden_chunk_dir(), '*.zz')
  return sorted(tf.gfile.Glob(pattern), reverse=True)[:num_records]


# Self-play a number of games.
async def selfplay(state, flagfile='selfplay'):
  """Run selfplay and write a training chunk to the fsdb golden_chunk_dir.

  Args:
    state: the RL loop State instance.
    flagfile: the name of the flagfile to use for selfplay, either 'selfplay'
        (the default) or 'boostrap'.
  """

  output_dir = os.path.join(fsdb.selfplay_dir(), state.output_model_name)
  holdout_dir = os.path.join(fsdb.holdout_dir(), state.output_model_name)

  lines = await checked_run(
      'bazel-bin/cc/selfplay',
      '--flagfile={}.flags'.format(os.path.join(FLAGS.flags_dir, flagfile)),
      '--model={}'.format(state.best_model_path),
      '--output_dir={}'.format(output_dir),
      '--holdout_dir={}'.format(holdout_dir),
      '--seed={}'.format(state.seed))
  result = '\n'.join(lines[-12:])
  print("result = ", result)
  logging.info(result)
  stats = parse_win_stats_table(result, 1)[0]
  num_games = stats.total_wins
  logging.info('Black won %0.3f, white won %0.3f',
               stats.black_wins.total / num_games,
               stats.white_wins.total / num_games)

  # Write examples to a single record.
  pattern = os.path.join(output_dir, '*', '*.zz')
  random.seed(state.seed)
  tf.set_random_seed(state.seed)
  np.random.seed(state.seed)
  # TODO(tommadams): This method of generating one golden chunk per generation
  # is sub-optimal because each chunk gets reused multiple times for training,
  # introducing bias. Instead, a fresh dataset should be uniformly sampled out
  # of *all* games in the training window before the start of each training run.
  buffer = example_buffer.ExampleBuffer(sampling_frac=1.0)

  # TODO(tommadams): parallel_fill is currently non-deterministic. Make it not
  # so.
  logging.info('Writing golden chunk from "{}"'.format(pattern))
  buffer.parallel_fill(tf.gfile.Glob(pattern))
  buffer.flush(os.path.join(fsdb.golden_chunk_dir(),
                            state.output_model_name + '.tfrecord.zz'))


async def train(state, tf_records):
  """Run training and write a new model to the fsdb models_dir.

  Args:
    state: the RL loop State instance.
    tf_records: a list of paths to TensorFlow records to train on.
  """

  model_path = os.path.join(fsdb.models_dir(), state.train_model_name)
  await checked_run(
      'python3', 'train.py', *tf_records,
      '--flagfile={}'.format(os.path.join(FLAGS.flags_dir, 'train.flags')),
      '--work_dir={}'.format(fsdb.working_dir()),
      '--export_path={}'.format(model_path),
      '--training_seed={}'.format(state.seed),
      '--freeze=true')
  # Append the time elapsed from when the RL was started to when this model
  # was trained.
  elapsed = time.time() - state.start_time
  timestamps_path = os.path.join(FLAGS.log_dir, 'train_times.txt')
  with gfile.Open(timestamps_path, 'a') as f:
    print('{:.3f} {}'.format(elapsed, state.train_model_name), file=f)


async def validate(state, holdout_glob):
  """Validate the trained model against holdout games.

  Args:
    state: the RL loop State instance.
    holdout_glob: a glob that matches holdout games.
  """

  await checked_run(
      'python3', 'validate.py', holdout_glob,
      '--flagfile={}'.format(os.path.join(FLAGS.flags_dir, 'validate.flags')),
      '--work_dir={}'.format(fsdb.working_dir()))


async def evaluate_model(eval_model_path, target_model_path, sgf_dir, seed):
  """Evaluate one model against a target.

  Args:
    eval_model_path: the path to the model to evaluate.
    target_model_path: the path to the model to compare to.
    sgf_dif: directory path to write SGF output to.
    seed: random seed to use when running eval.

  Returns:
    The win-rate of eval_model against target_model in the range [0, 1].
  """

  # TODO(tommadams): Don't append .pb to model name for random model.
  lines = await checked_run(
      'bazel-bin/cc/eval',
      '--flagfile={}'.format(os.path.join(FLAGS.flags_dir, 'eval.flags')),
      '--model={}'.format(eval_model_path),
      '--model_two={}'.format(target_model_path),
      '--sgf_dir={}'.format(sgf_dir),
      '--seed={}'.format(seed))
  result = '\n'.join(lines[-12:])
  logging.info(result)
  eval_stats, target_stats = parse_win_stats_table(result, 2)
  num_games = eval_stats.total_wins + target_stats.total_wins
  win_rate = eval_stats.total_wins / num_games
  logging.info('Win rate %s vs %s: %.3f', eval_stats.model_name,
               target_stats.model_name, win_rate)
  return win_rate


async def evaluate_trained_model(state):
  """Evaluate the most recently trained model against the current best model.

  Args:
    state: the RL loop State instance.
  """

  return await evaluate_model(
      state.train_model_path, state.best_model_path,
      os.path.join(fsdb.eval_dir(), state.train_model_name), state.seed)


def rl_loop():
  """The main reinforcement learning (RL) loop."""

  state = State()

  # Play the first round of selfplay games with a fake model that returns
  # random noise. We do this instead of playing multiple games using a single
  # model bootstrapped with random noise to avoid any initial bias.
  wait(selfplay(state, 'bootstrap'))

  # Train a real model from the random selfplay games.
  tf_records = get_golden_chunk_records(1)
  state.iter_num += 1
  wait(train(state, tf_records))

  # Select the newly trained model as the best.
  state.best_model_name = state.train_model_name
  state.gen_num += 1

  # Run selfplay using the new model.
  wait(selfplay(state))

  # Now start the full training loop.
  while state.iter_num <= FLAGS.iterations:
    # Build holdout glob before incrementing the iteration number because we
    # want to run validation on the previous generation.
    holdout_glob = os.path.join(fsdb.holdout_dir(), '%06d-*' % state.iter_num,
                                '*')

    # Calculate the window size from which we'll select training chunks.
    window = 1 + state.iter_num
    if window >= FLAGS.slow_window_size:
      window = (FLAGS.slow_window_size +
                (window - FLAGS.slow_window_size) // FLAGS.slow_window_speed)
    window = min(window, FLAGS.max_window_size)

    # Train on shuffled game data from recent selfplay rounds.
    tf_records = get_golden_chunk_records(window)
    state.iter_num += 1
    wait(train(state, tf_records))

    if FLAGS.parallel_post_train:
      # Run eval, validation & selfplay in parallel.
      model_win_rate, _, _ = wait([
          evaluate_trained_model(state),
          validate(state, holdout_glob),
          selfplay(state)])
    else:
      # Run eval, validation & selfplay sequentially.
      model_win_rate = wait(evaluate_trained_model(state))
      wait(validate(state, holdout_glob))
      wait(selfplay(state))

    # TODO(tommadams): if a model doesn't get promoted after N iterations,
    # consider deleting the most recent N training checkpoints because training
    # might have got stuck in a local minima.
    if model_win_rate >= FLAGS.gating_win_rate:
      # Promote the trained model to the best model and increment the generation
      # number.
      state.best_model_name = state.train_model_name
      state.gen_num += 1


def main(unused_argv):
  """Run the reinforcement learning loop."""

  base_dir = fsdb.base_dir()
  print('Base_dir %s' % base_dir, flush=True)
  if tf.gfile.Exists(base_dir):
    tf.gfile.DeleteRecursively(base_dir)
     
  tf.gfile.MakeDirs(fsdb.base_dir())
  tf.gfile.MakeDirs(fsdb.models_dir())
  tf.gfile.MakeDirs(fsdb.selfplay_dir())
  tf.gfile.MakeDirs(fsdb.holdout_dir())
  tf.gfile.MakeDirs(fsdb.eval_dir())
  tf.gfile.MakeDirs(fsdb.golden_chunk_dir())
  tf.gfile.MakeDirs(fsdb.working_dir())

  # Copy the flag files so there's no chance of them getting accidentally
  # overwritten while the RL loop is running.
  flags_dir = os.path.join(base_dir, 'flags')
  shutil.copytree(FLAGS.flags_dir, flags_dir)
  FLAGS.flags_dir = flags_dir

  # Copy the target model to the models directory so we can find it easily.
  tf.gfile.Copy('ml_perf/target.pb', fsdb.models_dir() + '/target.pb')

  logging.getLogger().addHandler(
      logging.FileHandler(os.path.join(FLAGS.log_dir, 'rl_loop.log')))
  formatter = logging.Formatter('[%(asctime)s] %(message)s',
                                '%Y-%m-%d %H:%M:%S')
  for handler in logging.getLogger().handlers:
    handler.setFormatter(formatter)

  with utils.logged_timer('Total time'):
    try:
      rl_loop()
    finally:
      asyncio.get_event_loop().close()


if __name__ == '__main__':
  app.run(main)
