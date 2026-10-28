"""Foley: model-free sound effects rendered as code (a fork of the MIT-licensed earshot sound bank, see NOTICE).

The forge package holds 252 effects made of recipes, their kits (a sound per role with measured sync points) and the
audits that check a take. Nothing here loads a model or needs a network; rendering is deterministic."""
__version__ = '0.4.0'   # the upstream release this fork was taken from; kit locks record it
