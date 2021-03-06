import expr
from itertools import repeat
from context import Symbol, Scope
from type_objects import List, Dict, Unknown, Function, NoneType, Instance
from util import type_intersection
from evaluate import UnknownValue


def get_token(node):
    return node.__class__.__name__


class NullEvaluator(object):  # for recursion
    def evaluate(self, argument_scope):
        _ = argument_scope
        return Unknown(), UnknownValue()


# "arguments" parameter is node.args for FunctionDef or Lambda
class FunctionSignature(object):
    def __init__(self, name=None, arguments=None, context=None,
                 decorator_list=[]):
        self.name = name
        if arguments is None:
            self.names = []
            self.types = []
            self.default_types = []
            self.annotated_types = []
            self.vararg_name = None
            self.kwarg_name = None
            self.min_count = 0
            return      # only for copy constructor below
        assert context is not None
        self.names = [arg.id for arg in arguments.args]
        self.min_count = len(arguments.args) - len(arguments.defaults)
        default_types = [expr.expression_type(d, context)
                         for d in arguments.defaults]
        self.default_types = ([Unknown()] * self.min_count) + default_types
        self.annotated_types = self._get_annotated_types(
            decorator_list, context)
        self.types = [annotated if annotated != Unknown() else default
                      for annotated, default
                      in zip(self.annotated_types, self.default_types)]
        self.vararg_name = arguments.vararg
        self.kwarg_name = arguments.kwarg

    def __hash__(self):
        return hash(tuple(self.types))

    def __contains__(self, name):
        return name in self.names

    def __len__(self):
        return len(self.names)

    def __getitem__(self, index):
        return zip(self.names, self.types)[index]

    @classmethod
    def copy_without_first_argument(cls, other_arguments):
        arguments = cls()
        arguments.names = other_arguments.names[1:]
        arguments.types = other_arguments.types[1:]
        arguments.default_types = other_arguments.default_types[1:]
        arguments.annotated_types = other_arguments.annotated_types[1:]
        arguments.min_count = max(0, other_arguments.min_count - 1)
        arguments.vararg_name = other_arguments.vararg_name
        arguments.kwarg_name = other_arguments.kwarg_name
        return arguments

    def constrain_type(self, name, type_):
        for i, arg_name in enumerate(self.names):
            if arg_name == name:
                intersection = type_intersection(type_, self.types[i])
                if intersection is not None:
                    self.types[i] = intersection
                    return intersection
                else:
                    return None

    def constrain_types(self, constraints):
        for name in self.names:
            if name in constraints:
                self.constrain_type(name, constraints[name])

    def get_list(self):
        return zip(self.names, self.types)

    def get_dict(self):
        return dict(self.get_list())

    def _get_annotated_types(self, decorator_list, context):
        types_decorator = [d for d in decorator_list
                           if get_token(d) == 'Call' and d.func.id == 'types']
        return ([expr.expression_type(arg, context)
                 for arg in types_decorator[0].args]
                if len(types_decorator) == 1 else repeat(Unknown()))

    def generic_scope(self):
        scope = Scope()
        if self.name is not None:   # for recursive calls
            function_type = Function(self, Unknown(), NullEvaluator())
            scope.add(Symbol(self.name, function_type))
        for name, argtype in self.get_dict().items():
            scope.add(Symbol(name, argtype))
        if self.vararg_name:
            scope.add(Symbol(self.vararg_name, List(Unknown())))
        if self.kwarg_name:
            scope.add(Symbol(self.kwarg_name, Dict(Unknown(), Unknown())))
        return scope

    def __str__(self):
        vararg = (', {0}: Tuple'.format(self.vararg_name)
                  if self.vararg_name else '')
        kwarg = (', {0}: Dict'.format(self.kwarg_name)
                 if self.kwarg_name else '')
        return (', '.join(name + ': ' + str(argtype) for name, argtype
                          in zip(self.names, self.types)) + vararg + kwarg)


# we only generate warnings on the first pass through a function definition
# the FunctionEvaluator is only to evaluate the type and static value of
# function calls
class FunctionEvaluator(object):
    def __init__(self, body, visitor):
        self._body = body
        self._visitor = visitor
        self._recursion_block = False

    def _evaluate(self, argument_scope):
        visitor = self._visitor
        visitor.begin_scope()
        visitor.merge_scope(argument_scope)
        if isinstance(self._body, list):
            for stmt in self._body:
                visitor.visit(stmt)
        else:
            visitor.visit(self._body)
        return visitor.end_scope()

    def evaluate(self, argument_scope):
        if self._recursion_block:
            return Unknown(), UnknownValue()
        self._recursion_block = True
        if self._body is None:
            return NoneType(), None
        scope = self._evaluate(argument_scope)
        self._recursion_block = False
        return_type = scope.get_type() or NoneType()
        if return_type != NoneType():
            return_value = scope.get_value() or UnknownValue()
        else:
            return_value = None
        return return_type, return_value


class ClassEvaluator(object):
    def __init__(self, class_object):
        self._class_object = class_object

    def evaluate(self, argument_scope):
        # argument_scope does not contain "self" parameter at this point
        # because we create the "self" instance inside this method
        instance = Instance(self._class_object.name, Scope())
        class_attributes = self._class_object.attributes
        for name in class_attributes.names():
            symbol_type = class_attributes.get_type(name)
            if isinstance(symbol_type, Function):
                function_type = Function(
                    symbol_type.signature,
                    symbol_type.return_type,
                    symbol_type.evaluator, instance)
                instance.attributes.add(Symbol(name, function_type))

        # Note: error checking for arguments passed in has already been
        # handled because the signature for the class object is loaded
        # based on the signature of the __init__ function
        init_function_type = instance.attributes.get_type('__init__')
        if init_function_type is not None:
            symbol = Symbol(init_function_type.signature.names[0], instance)
            argument_scope.add(symbol)
            init_function_type.evaluator.evaluate(argument_scope)
        instance.initialized = True
        return instance, UnknownValue()


# problem: where are we going to check for errors in the function call?
def construct_function_type(functiondef_node, visitor, instance=None):
    name = getattr(functiondef_node, 'name', None)
    signature = FunctionSignature(name, functiondef_node.args,
                                  visitor.context())
    body = functiondef_node.body
    first_visitor = (visitor if instance is None
                     or (name == '__init__' and not instance.initialized)
                     or (name != '__init__' and instance.initialized)
                     else visitor.clone())
    first_evaluator = FunctionEvaluator(body, first_visitor)
    first_visitor.context().clear_constraints()
    argument_scope = signature.generic_scope()
    if instance is not None:
        self_symbol = Symbol(signature.names[0], instance)
        argument_scope.add(self_symbol)
    return_type, _ = first_evaluator.evaluate(argument_scope)
    signature.constrain_types(first_visitor.context().get_constraints())
    evaluator = FunctionEvaluator(body, visitor.clone())
    return Function(signature, return_type, evaluator)
