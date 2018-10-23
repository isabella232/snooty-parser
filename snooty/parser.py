import abc
import docutils.frontend
import docutils.nodes
import docutils.parsers.rst
import docutils.parsers.rst.directives
import docutils.parsers.rst.roles
import docutils.parsers.rst.states
import docutils.statemachine
import docutils.utils
import re
import sys
from dataclasses import dataclass
from typing import Callable, Dict, Generic, Optional, List, Tuple, Type, TypeVar
from .gizaparser import load_yaml
from .gizaparser.flutter import checked, check_type, LoadError

PAT_EXPLICIT_TILE = re.compile(r'^(?P<label>.+?)\s*(?<!\x00)<(?P<target>.*?)>$', re.DOTALL)
PAT_WHITESPACE = re.compile(r'^\x20*')
SPECIAL_DIRECTIVES = {'code-block', 'include', 'tabs-drivers', 'tabs', 'tabs-platforms', 'only'}


@checked
@dataclass
class LegacyTabDefinition:
    __line__: int
    id: str
    title: Optional[str]
    content: str


@checked
@dataclass
class LegacyTabsDefinition:
    __line__: int
    hidden: Optional[bool]
    tabs: List[LegacyTabDefinition]


class directive_argument(docutils.nodes.General, docutils.nodes.TextElement):
    pass


class directive(docutils.nodes.General, docutils.nodes.Element):
    def __init__(self, name: str) -> None:
        super(directive, self).__init__()
        self['name'] = name


class role(docutils.nodes.General, docutils.nodes.Inline, docutils.nodes.Element):
    def __init__(self, name: str, rawtext: str, text: str, lineno: int) -> None:
        super(role, self).__init__()
        self['name'] = name
        self['raw'] = text

        match = PAT_EXPLICIT_TILE.match(text)
        if match:
            self['label'] = {
                'type': 'text',
                'value': match['label'],
                'position': {'start': {'line': lineno}}
            }
            self['target'] = match['target']
        else:
            self['label'] = text
            self['target'] = text


def parse_options(block_text: str) -> Dict[str, str]:
    """Docutils doesn't parse directive options that aren't known ahead
       of time. Do it ourselves, badly."""
    lines = block_text.split('\n')
    current_key: Optional[str] = None
    kv: Dict[str, str] = {}
    base_indentation = 0

    for i, line in enumerate(lines):
        if i == 0:
            continue

        stripped = line.strip()
        if not stripped:
            continue

        whitespace_match = PAT_WHITESPACE.match(line)
        assert whitespace_match is not None
        indentation = len(whitespace_match.group(0))

        if base_indentation == 0:
            base_indentation = indentation

        match = re.match(docutils.parsers.rst.states.Body.patterns['field_marker'], stripped)
        if match:
            current_key = match.group(0)
            assert current_key is not None
            value = stripped[len(current_key):]
            current_key = current_key.strip().strip(':')
            kv[current_key] = value
            continue

        if indentation == base_indentation:
            break
        elif current_key:
            kv[current_key] += '\n' + line[indentation:]

    return kv


class Directive(docutils.parsers.rst.Directive):
    optional_arguments = 1
    final_argument_whitespace = True
    has_content = True

    def run(self) -> List[docutils.nodes.Node]:
        messages: List[docutils.nodes.Node] = []
        source, line = self.state_machine.get_source_and_line(self.lineno)

        node = directive(self.name)
        node.document = self.state.document
        node.source, node.line = source, line
        self.add_name(node)

        # Parse the argument (i.e. what's after the colon on the 0th line)
        if self.arguments:
            argument_text = self.arguments[0].split('\n')[0]
            textnodes, messages = self.state.inline_text(argument_text, self.lineno)
            argument = directive_argument(argument_text, '', *textnodes)
            argument.document = self.state.document
            argument.source, argument.line = source, line
            node.append(argument)

        # Parse options
        node['options'] = parse_options(self.block_text)

        # Parse the content
        if self.name in SPECIAL_DIRECTIVES:
            raw = docutils.nodes.FixedTextElement()
            raw.document = self.state.document
            raw.source, raw.line = source, line
            node.append(raw)
        else:
            self.state.nested_parse(self.content, self.state_machine.line_offset, node)

        return [node]


def prepare_viewlist(text: str, ignore: int=1) -> List[str]:
    """Convert a docstring into lines of parseable reST.  Remove common leading
    indentation, where the indentation of a given number of lines (usually just
    one) is ignored.
    Return the docstring as a list of lines usable for inserting into a docutils
    ViewList (used as argument of nested_parse().)  An empty line is added to
    act as a separator between this docstring and following content.
    """
    lines = text.expandtabs().splitlines()
    # Find minimum indentation of any non-blank lines after ignored lines.
    margin = sys.maxsize
    for line in lines[ignore:]:
        content = len(line.lstrip())
        if content:
            indent = len(line) - content
            margin = min(margin, indent)
    # Remove indentation from ignored lines.
    for i in range(ignore):
        if i < len(lines):
            lines[i] = lines[i].lstrip()
    if margin < sys.maxsize:
        for i in range(ignore, len(lines)):
            lines[i] = lines[i][margin:]
    # Remove any leading blank lines.
    while lines and not lines[0]:
        lines.pop(0)
    # make sure there is an empty line at the end
    if lines and lines[-1]:
        lines.append('')
    return lines


class TabsDirective(Directive):
    option_spec = {
        'tabset': str,
        'hidden': bool
    }
    has_content = True

    def run(self) -> List[docutils.nodes.Node]:
        # Transform the old YAML-based syntax into the new pure-rst syntax.
        # This heuristic gusses whether we have the old syntax or the NEW.
        if any(line == 'tabs:' for line in self.content):
            parsed = load_yaml('\n'.join(self.content))[0]
            try:
                loaded = check_type(LegacyTabsDefinition, parsed)
            except LoadError as err:
                line = self.lineno + getattr(err.bad_data, '__line__', 0) + 1
                error_node = self.state.document.reporter.error(
                    str(err),
                    line=line)
                return [error_node]

            tabset = self.name.split('-', 1)[-1]
            node = directive('tabs')
            node.document = self.state.document
            source, node.line = self.state_machine.get_source_and_line(self.lineno)
            node.source = source
            self.add_name(node)

            options: Dict[str, object] = {}
            node['options'] = options
            if loaded.hidden:
                options['hidden'] = True

            if tabset and tabset != 'tabs':
                options['tabset'] = tabset

            for child in loaded.tabs:
                node.append(self.make_tab_node(source, child))

            return [node]

        # The new syntax needs no special handling
        return Directive.run(self)

    def make_tab_node(self, source: str, child: LegacyTabDefinition) -> docutils.nodes.Node:
        line = self.lineno + child.__line__

        node = directive('tab')
        node.document = self.state.document
        node.source = source
        node.line = line

        argument_text = child.id
        textnodes, messages = self.state.inline_text(argument_text, line)
        argument = directive_argument(argument_text, '', *textnodes)
        argument.document = self.state.document
        argument.source, argument.line = source, line
        node.append(argument)
        node['options'] = {}

        content_lines = prepare_viewlist(child.content)
        self.state.nested_parse(
            docutils.statemachine.ViewList(content_lines, source=source),
            self.state_machine.line_offset,
            node)

        return node


def handle_role(typ: str, rawtext: str, text: str,
                lineno: int, inliner: object,
                options: Dict={}, content: List=[]) -> Tuple[List, List]:
    node = role(typ, rawtext, text, lineno)
    return [node], []


def lookup_directive(directive_name: str, language_module: object,
                     document: docutils.nodes.document) -> Tuple[Type, List]:
    if directive_name.startswith('tabs'):
        return TabsDirective, []

    return Directive, []


def lookup_role(role_name: str, language_module: object, lineno: int,
                reporter: object) -> Tuple[Optional[Callable], List]:
    return handle_role, []


docutils.parsers.rst.directives.directive = lookup_directive
docutils.parsers.rst.roles.role = lookup_role


class NoTransformRstParser(docutils.parsers.rst.Parser):
    def get_transforms(self) -> List:
        return []


class Visitor(metaclass=abc.ABCMeta):
    def __init__(self, document: docutils.nodes.document) -> None: pass

    @abc.abstractmethod
    def dispatch_visit(self, node: docutils.nodes.Node) -> None:
        pass

    @abc.abstractmethod
    def dispatch_departure(self, node: docutils.nodes.Node) -> None:
        pass


V = TypeVar('V', bound=Visitor)


class Parser(Generic[V]):
    __slots__ = ('visitor_class', 'directives')

    def __init__(self, visitor_class: Type[V]) -> None:
        self.visitor_class = visitor_class

    def parse(self, path: str, text: str) -> V:
        parser = NoTransformRstParser()
        settings = docutils.frontend.OptionParser(
            components=(docutils.parsers.rst.Parser,)
            ).get_default_values()
        settings.report_level = docutils.utils.Reporter.SEVERE_LEVEL
        document = docutils.utils.new_document(path, settings)
        parser.parse(text, document)

        visitor = self.visitor_class(document)
        document.walkabout(visitor)
        return visitor
