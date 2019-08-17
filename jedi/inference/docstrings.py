"""
Docstrings are another source of information for functions and classes.
:mod:`jedi.inference.dynamic` tries to find all executions of functions, while
the docstring parsing is much easier. There are three different types of
docstrings that |jedi| understands:

- `Sphinx <http://sphinx-doc.org/markup/desc.html#info-field-lists>`_
- `Epydoc <http://epydoc.sourceforge.net/manual-fields.html>`_
- `Numpydoc <https://github.com/numpy/numpy/blob/master/doc/HOWTO_DOCUMENT.rst.txt>`_

For example, the sphinx annotation ``:type foo: str`` clearly states that the
type of ``foo`` is ``str``.

As an addition to parameter searching, this module also provides return
annotations.
"""

import re
import warnings
from textwrap import dedent

from parso import parse, ParserSyntaxError

from jedi._compatibility import u
from jedi import debug
from jedi.inference.utils import indent_block
from jedi.inference.cache import inference_state_method_cache
from jedi.inference.base_value import iterator_to_value_set, ValueSet, \
    NO_VALUES
from jedi.inference.lazy_value import LazyKnownValues


DOCSTRING_PARAM_PATTERNS = [
    r'\s*:type\s+%s:\s*([^\n]+)',  # Sphinx
    r'\s*:param\s+(\w+)\s+%s:[^\n]*',  # Sphinx param with type
    r'\s*@type\s+%s:\s*([^\n]+)',  # Epydoc
]

DOCSTRING_RETURN_PATTERNS = [
    re.compile(r'\s*:rtype:\s*([^\n]+)', re.M),  # Sphinx
    re.compile(r'\s*@rtype:\s*([^\n]+)', re.M),  # Epydoc
]

REST_ROLE_PATTERN = re.compile(r':[^`]+:`([^`]+)`')


_numpy_doc_string_cache = None


def _get_numpy_doc_string_cls():
    global _numpy_doc_string_cache
    if isinstance(_numpy_doc_string_cache, (ImportError, SyntaxError)):
        raise _numpy_doc_string_cache
    from numpydoc.docscrape import NumpyDocString
    _numpy_doc_string_cache = NumpyDocString
    return _numpy_doc_string_cache


def _search_param_in_numpydocstr(docstr, param_str):
    """Search `docstr` (in numpydoc format) for type(-s) of `param_str`."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            # This is a non-public API. If it ever changes we should be
            # prepared and return gracefully.
            params = _get_numpy_doc_string_cls()(docstr)._parsed_data['Parameters']
        except Exception:
            return []
    for p_name, p_type, p_descr in params:
        if p_name == param_str:
            m = re.match(r'([^,]+(,[^,]+)*?)(,[ ]*optional)?$', p_type)
            if m:
                p_type = m.group(1)
            return list(_expand_typestr(p_type))
    return []


def _search_return_in_numpydocstr(docstr):
    """
    Search `docstr` (in numpydoc format) for type(-s) of function returns.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            doc = _get_numpy_doc_string_cls()(docstr)
        except Exception:
            return
    try:
        # This is a non-public API. If it ever changes we should be
        # prepared and return gracefully.
        returns = doc._parsed_data['Returns']
        returns += doc._parsed_data['Yields']
    except Exception:
        return
    for r_name, r_type, r_descr in returns:
        # Return names are optional and if so the type is in the name
        if not r_type:
            r_type = r_name
        for type_ in _expand_typestr(r_type):
            yield type_


def _expand_typestr(type_str):
    """
    Attempts to interpret the possible types in `type_str`
    """
    # Check if alternative types are specified with 'or'
    if re.search(r'\bor\b', type_str):
        for t in type_str.split('or'):
            yield t.split('of')[0].strip()
    # Check if like "list of `type`" and set type to list
    elif re.search(r'\bof\b', type_str):
        yield type_str.split('of')[0]
    # Check if type has is a set of valid literal values eg: {'C', 'F', 'A'}
    elif type_str.startswith('{'):
        node = parse(type_str, version='3.7').children[0]
        if node.type == 'atom':
            for leaf in node.children[1].children:
                if leaf.type == 'number':
                    if '.' in leaf.value:
                        yield 'float'
                    else:
                        yield 'int'
                elif leaf.type == 'string':
                    if 'b' in leaf.string_prefix.lower():
                        yield 'bytes'
                    else:
                        yield 'str'
                # Ignore everything else.

    # Otherwise just work with what we have.
    else:
        yield type_str


def _search_param_in_docstr(docstr, param_str):
    """
    Search `docstr` for type(-s) of `param_str`.

    >>> _search_param_in_docstr(':type param: int', 'param')
    ['int']
    >>> _search_param_in_docstr('@type param: int', 'param')
    ['int']
    >>> _search_param_in_docstr(
    ...   ':type param: :class:`threading.Thread`', 'param')
    ['threading.Thread']
    >>> bool(_search_param_in_docstr('no document', 'param'))
    False
    >>> _search_param_in_docstr(':param int param: some description', 'param')
    ['int']

    """
    # look at #40 to see definitions of those params
    patterns = [re.compile(p % re.escape(param_str))
                for p in DOCSTRING_PARAM_PATTERNS]
    for pattern in patterns:
        match = pattern.search(docstr)
        if match:
            return [_strip_rst_role(match.group(1))]

    return _search_param_in_numpydocstr(docstr, param_str)


def _strip_rst_role(type_str):
    """
    Strip off the part looks like a ReST role in `type_str`.

    >>> _strip_rst_role(':class:`ClassName`')  # strip off :class:
    'ClassName'
    >>> _strip_rst_role(':py:obj:`module.Object`')  # works with domain
    'module.Object'
    >>> _strip_rst_role('ClassName')  # do nothing when not ReST role
    'ClassName'

    See also:
    http://sphinx-doc.org/domains.html#cross-referencing-python-objects

    """
    match = REST_ROLE_PATTERN.match(type_str)
    if match:
        return match.group(1)
    else:
        return type_str


def _infer_for_statement_string(module_value, string):
    code = dedent(u("""
    def pseudo_docstring_stuff():
        '''
        Create a pseudo function for docstring statements.
        Need this docstring so that if the below part is not valid Python this
        is still a function.
        '''
    {}
    """))
    if string is None:
        return []

    for element in re.findall(r'((?:\w+\.)*\w+)\.', string):
        # Try to import module part in dotted name.
        # (e.g., 'threading' in 'threading.Thread').
        string = 'import %s\n' % element + string

    # Take the default grammar here, if we load the Python 2.7 grammar here, it
    # will be impossible to use `...` (Ellipsis) as a token. Docstring types
    # don't need to conform with the current grammar.
    debug.dbg('Parse docstring code %s', string, color='BLUE')
    grammar = module_value.inference_state.latest_grammar
    try:
        module = grammar.parse(code.format(indent_block(string)), error_recovery=False)
    except ParserSyntaxError:
        return []
    try:
        funcdef = next(module.iter_funcdefs())
        # First pick suite, then simple_stmt and then the node,
        # which is also not the last item, because there's a newline.
        stmt = funcdef.children[-1].children[-1].children[-2]
    except (AttributeError, IndexError):
        return []

    if stmt.type not in ('name', 'atom', 'atom_expr'):
        return []

    from jedi.inference.value import FunctionValue
    function_value = FunctionValue(
        module_value.inference_state,
        module_value,
        funcdef
    )
    func_execution_value = function_value.get_function_execution()
    # Use the module of the param.
    # TODO this module is not the module of the param in case of a function
    # call. In that case it's the module of the function call.
    # stuffed with content from a function call.
    return list(_execute_types_in_stmt(func_execution_value, stmt))


def _execute_types_in_stmt(module_value, stmt):
    """
    Executing all types or general elements that we find in a statement. This
    doesn't include tuple, list and dict literals, because the stuff they
    contain is executed. (Used as type information).
    """
    definitions = module_value.infer_node(stmt)
    return ValueSet.from_sets(
        _execute_array_values(module_value.inference_state, d)
        for d in definitions
    )


def _execute_array_values(inference_state, array):
    """
    Tuples indicate that there's not just one return value, but the listed
    ones.  `(str, int)` means that it returns a tuple with both types.
    """
    from jedi.inference.value.iterable import SequenceLiteralValue, FakeSequence
    if isinstance(array, SequenceLiteralValue):
        values = []
        for lazy_value in array.py__iter__():
            objects = ValueSet.from_sets(
                _execute_array_values(inference_state, typ)
                for typ in lazy_value.infer()
            )
            values.append(LazyKnownValues(objects))
        return {FakeSequence(inference_state, array.array_type, values)}
    else:
        return array.execute_annotation()


@inference_state_method_cache()
def infer_param(execution_value, param):
    from jedi.inference.value.instance import InstanceArguments
    from jedi.inference.value import FunctionExecutionContext

    def infer_docstring(docstring):
        return ValueSet(
            p
            for param_str in _search_param_in_docstr(docstring, param.name.value)
            for p in _infer_for_statement_string(module_context, param_str)
        )
    module_context = execution_value.get_root_context()
    func = param.get_parent_function()
    if func.type == 'lambdef':
        return NO_VALUES

    types = infer_docstring(execution_value.py__doc__())
    if isinstance(execution_value, FunctionExecutionContext) \
            and isinstance(execution_value.var_args, InstanceArguments) \
            and execution_value.function_value.py__name__() == '__init__':
        class_value = execution_value.var_args.instance.class_value
        types |= infer_docstring(class_value.py__doc__())

    debug.dbg('Found param types for docstring: %s', types, color='BLUE')
    return types


@inference_state_method_cache()
@iterator_to_value_set
def infer_return_types(function_value):
    def search_return_in_docstr(code):
        for p in DOCSTRING_RETURN_PATTERNS:
            match = p.search(code)
            if match:
                yield _strip_rst_role(match.group(1))
        # Check for numpy style return hint
        for type_ in _search_return_in_numpydocstr(code):
            yield type_

    for type_str in search_return_in_docstr(function_value.py__doc__()):
        for value in _infer_for_statement_string(function_value.get_root_context(), type_str):
            yield value
