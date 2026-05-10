"""
Parser factory: returns the correct parser for a given language.
"""
from __future__ import annotations

from ..config import Config
from ..models import Language
from .base import BaseParser


def get_parser(language: Language, config: Config) -> BaseParser:
    if language == Language.PYTHON:
        from .python_parser import PythonParser
        return PythonParser(config)
    elif language == Language.C:
        from .c_parser import CParser
        return CParser(config)
    elif language == Language.CPP:
        from .cpp_parser import CppParser
        return CppParser(config)
    elif language == Language.MATLAB:
        from .matlab_parser import MatlabParser
        return MatlabParser(config)
    else:
        raise ValueError(f"No parser registered for language: {language}")
