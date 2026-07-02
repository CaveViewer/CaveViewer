"""
core/setup_drawbatch.py

Builds the drawbatch C extension in-place.

Run from the project root:
    python core/setup_drawbatch.py build_ext --inplace

The resulting .so / .pyd file lands in core/ and is importable as:
    from core import drawbatch

No OpenGL headers are needed at compile time -- GL function pointers are
passed in as plain integers from the Python side at runtime.
"""
from setuptools import setup, Extension

setup(
    name="drawbatch",
    ext_modules=[
        Extension(
            name="core.drawbatch",
            sources=["core/drawbatch.c"],
        )
    ],
)
