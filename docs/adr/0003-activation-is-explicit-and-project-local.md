# Keep activation explicit and project-local

A committed Binding records project intent, but generated, uncommitted symlinks
perform Activation only after a complete plan is reviewed and explicitly applied.
This separates portable configuration from machine-local paths, prevents silent
mutation, and protects project-owned Skills; only the future Router is eligible for
global installation.
