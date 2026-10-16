"""Textos bilingües: cada t("es", "en") debe tener los mismos marcadores {…} en los dos idiomas (si no, el
idioma que falla lanza KeyError en plena ejecución) y el módulo de idioma debe comportarse como se espera."""
import ast
import string
from pathlib import Path

import pytest

from rngauto import i18n

SRC = Path(__file__).resolve().parents[1] / "src" / "rngauto"


def _fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def _t_calls():
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if name != "t" or len(node.args) < 2:
                continue
            es, en = node.args[:2]
            if isinstance(es, ast.Constant) and isinstance(en, ast.Constant) and isinstance(es.value, str):
                yield path, node.lineno, es.value, en.value, {k.arg for k in node.keywords if k.arg}


def test_every_translation_has_the_same_placeholders_and_arguments():
    problems = []
    for path, line, es, en, kwargs in _t_calls():
        try:
            fe, fn = _fields(es), _fields(en)
        except ValueError as exc:  # llave suelta
            problems.append(f"{path.name}:{line}: formato inválido ({exc})")
            continue
        if fe != fn:
            problems.append(f"{path.name}:{line}: {sorted(fe)} ≠ {sorted(fn)}")
        elif kwargs and not fe <= kwargs:
            problems.append(f"{path.name}:{line}: faltan argumentos {sorted(fe - kwargs)}")
    assert not problems, "\n".join(problems)


def test_there_are_translations():
    assert sum(1 for _ in _t_calls()) > 500


def test_language_switch_and_formatting():
    i18n.set_language("en")
    assert i18n.t("Hola {n}", "Hello {n}", n=3) == "Hello 3"
    i18n.set_language("es")
    assert i18n.t("Hola {n}", "Hello {n}", n=3) == "Hola 3"
    i18n.set_language("xx")  # desconocido: el idioma por defecto
    assert i18n.language() == i18n.DEFAULT


@pytest.mark.parametrize("values,expected", [({"es": "uno", "en": "one"}, "one"), ({"es": "uno"}, "uno"), (None, "x")])
def test_pick(values, expected):
    i18n.set_language("en")
    assert i18n.pick(values, "x") == expected
