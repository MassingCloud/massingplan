"""Adoption kits for the products that consume this engine.

Each subpackage holds everything a consumer needs to take the engine, kept here
rather than there. The adapter changes when *this* repo changes -- a new
relationship type, a renamed row key -- so it lives with the thing that moves,
and the consumer's adoption stays one command on their own branch.
"""
