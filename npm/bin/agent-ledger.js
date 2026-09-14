#!/usr/bin/env node
/**
 * agent-ledger — npx launcher for the AgentLedger CLI.
 *
 * The real implementation is Python and lives on PyPI as `aiagentscity-ledger`.
 * This launcher exists so an agent or human can run:
 *
 *     npx -p aiagentscity-ledger agent-ledger init
 *
 * on any machine with node + python3, without knowing anything about pip.
 * It bootstraps a dedicated venv under ~/.agentledger/venv (once), installs the
 * PyPI package into it, then execs the CLI from there. No logic lives here —
 * this file only bootstraps. There is exactly one implementation.
 */
'use strict';

const { execFileSync, spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const PACKAGE = 'aiagentscity-ledger';
const VENV_DIR = path.join(os.homedir(), '.agentledger', 'venv');

function python3() {
  for (const candidate of [
    process.env.AL_PYTHON, // explicit override wins
    '/usr/local/bin/python3',
    '/opt/homebrew/bin/python3',
    '/opt/miniconda3/bin/python3',
    'python3',
  ].filter(Boolean)) {
    const probe = spawnSync(candidate, ['--version'], { encoding: 'utf8' });
    if (probe.status === 0) return candidate;
  }
  console.error('agent-ledger: no python3 found. Install Python 3.9+ and retry.');
  process.exit(1);
}

function venvReady(py) {
  const bin = path.join(VENV_DIR, 'bin', 'agent-ledger');
  if (process.platform === 'win32') {
    return fs.existsSync(path.join(VENV_DIR, 'Scripts', 'agent-ledger.exe'));
  }
  if (fs.existsSync(bin)) {
    // Cheap version check: does the installed CLI run at all?
    const probe = spawnSync(bin, ['--help'], { encoding: 'utf8' });
    return probe.status === 0;
  }
  return false;
}

function bootstrap(py) {
  fs.mkdirSync(path.dirname(VENV_DIR), { recursive: true });
  execFileSync(py, ['-m', 'venv', VENV_DIR], { stdio: 'inherit' });
  const pip = process.platform === 'win32'
    ? path.join(VENV_DIR, 'Scripts', 'pip.exe')
    : path.join(VENV_DIR, 'bin', 'pip');
  execFileSync(pip, ['install', '--quiet', '--upgrade', PACKAGE], { stdio: 'inherit' });
}

const py = python3();
if (!venvReady(py)) {
  bootstrap(py);
}
const cli = process.platform === 'win32'
  ? path.join(VENV_DIR, 'Scripts', 'agent-ledger.exe')
  : path.join(VENV_DIR, 'bin', 'agent-ledger');

const result = spawnSync(cli, process.argv.slice(2), { stdio: 'inherit' });
process.exit(result.status ?? 1);