# encoding: utf-8
#
# Copyright (C) 2010 Alec Thomas <alec@swapoff.org>
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution.
#
# Author: Alec Thomas <alec@swapoff.org>

"""Injector - Python dependency injection framework, inspired by Guice

:copyright: (c) 2012 by Alec Thomas
:license: BSD
"""

import functools
import inspect
import itertools
import logging
import sys
import threading
import types
from abc import ABCMeta, abstractmethod
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Collection,
    Dict,
    Generator,
    Generic,
    Iterable,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
    get_args,
    overload,
)

from typing import Annotated, NoReturn, _AnnotatedAlias, get_type_hints  # type: ignore[attr-defined]

__author__ = 'Alec Thomas <alec@swapoff.org>'
__version__ = '0.24.0'
__version_tag__ = ''

log = logging.getLogger('injector')
log.addHandler(logging.NullHandler())

if log.level == logging.NOTSET:
    log.setLevel(logging.WARN)

T = TypeVar('T')
K = TypeVar('K')
V = TypeVar('V')


def private(something: T) -> T:
    pass


CallableT = TypeVar('CallableT', bound=Callable)


def synchronized(lock: threading.RLock) -> Callable[[CallableT], CallableT]:
    def outside_wrapper(function: CallableT) -> CallableT:
        @functools.wraps(function)
        pass

    return outside_wrapper


lock = threading.RLock()


_inject_marker = object()
_noinject_marker = object()

InjectT = TypeVar('InjectT')
Inject = Annotated[InjectT, _inject_marker]
"""An experimental way to declare injectable dependencies utilizing a `PEP 593`_ implementation
in Python 3.9.

Those two declarations are equivalent::

    @inject
    def fun(t: SomeType) -> None:
        pass

    def fun(t: Inject[SomeType]) -> None:
        pass

The advantage over using :func:`inject` is that if you have some noninjectable parameters
it may be easier to spot what are they. Those two are equivalent::

    @inject
    @noninjectable('s')
    def fun(t: SomeType, s: SomeOtherType) -> None:
        pass

    def fun(t: Inject[SomeType], s: SomeOtherType) -> None:
        pass

.. seealso::

    Function :func:`get_bindings`
        A way to inspect how various injection declarations interact with each other.

.. versionadded:: 0.18.0
.. note:: Requires Python 3.9+.
.. note::

    If you're using mypy you need the version 0.750 or newer to fully type-check code using this
    construct.

.. _PEP 593: https://www.python.org/dev/peps/pep-0593/
"""

NoInject = Annotated[InjectT, _noinject_marker]
"""An experimental way to declare noninjectable dependencies utilizing a `PEP 593`_ implementation
in Python 3.9.

Since :func:`inject` declares all function's parameters to be injectable there needs to be a way
to opt out of it. This has been provided by :func:`noninjectable` but `noninjectable` suffers from
two issues:

* You need to repeat the parameter name
* The declaration may be relatively distance in space from the actual parameter declaration, thus
  hindering readability

`NoInject` solves both of those concerns, for example (those two declarations are equivalent)::

    @inject
    @noninjectable('b')
    def fun(a: TypeA, b: TypeB) -> None:
        pass

    @inject
    def fun(a: TypeA, b: NoInject[TypeB]) -> None:
        pass

.. seealso::

    Function :func:`get_bindings`
        A way to inspect how various injection declarations interact with each other.

.. versionadded:: 0.18.0
.. note:: Requires Python 3.9+.
.. note::

    If you're using mypy you need the version 0.750 or newer to fully type-check code using this
    construct.

.. _PEP 593: https://www.python.org/dev/peps/pep-0593/
"""


def reraise(original: Exception, exception: Exception, maximum_frames: int = 1) -> NoReturn:
    pass


class Error(Exception):
    """Base exception."""


class UnsatisfiedRequirement(Error):
    """Requirement could not be satisfied."""

    def __init__(self, owner: Optional[object], interface: type) -> None:
        super().__init__(owner, interface)
        self.owner = owner
        self.interface = interface

    def __str__(self) -> str:
        on = '%s has an ' % _describe(self.owner) if self.owner else ''
        return '%sunsatisfied requirement on %s' % (on, _describe(self.interface))


class CallError(Error):
    """Call to callable object fails."""

    def __str__(self) -> str:
        if len(self.args) == 1:
            return self.args[0]

        instance, method, args, kwargs, original_error, stack = self.args
        cls = instance.__class__.__name__ if instance is not None else ''

        full_method = '.'.join((cls, method.__name__)).strip('.')

        parameters = ', '.join(
            itertools.chain(
                (repr(arg) for arg in args), ('%s=%r' % (key, value) for (key, value) in kwargs.items())
            )
        )
        return 'Call to %s(%s) failed: %s (injection stack: %r)' % (
            full_method,
            parameters,
            original_error,
            [level[0] for level in stack],
        )


class CircularDependency(Error):
    """Circular dependency detected."""


class UnknownProvider(Error):
    """Tried to bind to a type whose provider couldn't be determined."""


class UnknownArgument(Error):
    """Tried to mark an unknown argument as noninjectable."""


class InvalidInterface(Error):
    """Cannot bind to the specified interface."""


class Provider(Generic[T]):
    """Provides class instances."""

    __metaclass__ = ABCMeta

    @abstractmethod
    def get(self, injector: 'Injector') -> T:
        raise NotImplementedError  # pragma: no cover


class ClassProvider(Provider, Generic[T]):
    """Provides instances from a given class, created using an Injector."""

    def __init__(self, cls: Type[T]) -> None:
        self._cls = cls

    def get(self, injector: 'Injector') -> T:
        pass


class CallableProvider(Provider, Generic[T]):
    """Provides something using a callable.

    The callable is called every time new value is requested from the provider.

    There's no need to explicitly use :func:`inject` or :data:`Inject` with the callable as it's
    assumed that, if the callable has annotated parameters, they're meant to be provided
    automatically. It wouldn't make sense any other way, as there's no mechanism to provide
    parameters to the callable at a later time, so either they'll be injected or there'll be
    a `CallError`.

    ::

        >>> class MyClass:
        ...     def __init__(self, value: int) -> None:
        ...         self.value = value
        ...
        >>> def factory():
        ...     print('providing')
        ...     return MyClass(42)
        ...
        >>> def configure(binder):
        ...     binder.bind(MyClass, to=CallableProvider(factory))
        ...
        >>> injector = Injector(configure)
        >>> injector.get(MyClass) is injector.get(MyClass)
        providing
        providing
        False
    """

    def __init__(self, callable: Callable[..., T]):
        self._callable = callable

    def get(self, injector: 'Injector') -> T:
        pass

    def __repr__(self) -> str:
        return '%s(%r)' % (type(self).__name__, self._callable)


class InstanceProvider(Provider, Generic[T]):
    """Provide a specific instance.

    ::

        >>> class MyType:
        ...     def __init__(self):
        ...         self.contents = []
        >>> def configure(binder):
        ...     binder.bind(MyType, to=InstanceProvider(MyType()))
        ...
        >>> injector = Injector(configure)
        >>> injector.get(MyType) is injector.get(MyType)
        True
        >>> injector.get(MyType).contents.append('x')
        >>> injector.get(MyType).contents
        ['x']
    """

    def __init__(self, instance: T) -> None:
        self._instance = instance

    def get(self, injector: 'Injector') -> T:
        pass

    def __repr__(self) -> str:
        return '%s(%r)' % (type(self).__name__, self._instance)


@private
class MultiBinder(Provider, Generic[T]):
    """Provide a list of instances via other Providers."""

    __metaclass__ = ABCMeta

    _multi_bindings: List['Binding']

    def __init__(self, parent: 'Binder') -> None:
        self._multi_bindings = []
        self._binder = Binder(parent.injector, auto_bind=False, parent=parent)

    @abstractmethod
    def multibind(
        self, interface: type, to: Any, scope: Union['ScopeDecorator', Type['Scope'], None]
    ) -> None:
        raise NotImplementedError

    def append(self, provider: Provider[T], scope: Type['Scope']) -> None:
        # HACK: generate a pseudo-type for this element in the list.
        # This is needed for scopes to work properly. Some, like the Singleton scope,
        # key instances by type, so we need one that is unique to this binding.
        pass

    def get_scoped_providers(self, injector: 'Injector') -> Generator[Provider[T], None, None]:
        pass

    def __repr__(self) -> str:
        return '%s(%r)' % (type(self).__name__, self._multi_bindings)


class MultiBindProvider(MultiBinder[List[T]]):
    """Used by :meth:`Binder.multibind` to flatten results of providers that
    return sequences."""

    def multibind(
        self, interface: type, to: Any, scope: Union['ScopeDecorator', Type['Scope'], None]
    ) -> None:
        pass

    def get(self, injector: 'Injector') -> List[T]:
        pass


class MapBindProvider(MultiBinder[Dict[str, T]]):
    """A provider for map bindings."""

    def multibind(
        self, interface: type, to: Any, scope: Union['ScopeDecorator', Type['Scope'], None]
    ) -> None:
        pass

    def get(self, injector: 'Injector') -> Dict[str, T]:
        pass


@private
class KeyValueProvider(Provider[Dict[str, T]]):
    def __init__(self, key: str, inner_provider: Provider[T]) -> None:
        self._key = key
        self._provider = inner_provider

    def get(self, injector: 'Injector') -> Dict[str, T]:
        pass


@dataclass
class _BindingBase:
    interface: type
    provider: Provider
    scope: Type['Scope']


@private
class Binding(_BindingBase):
    """A binding from an (interface,) to a provider in a scope."""

    def is_multibinding(self) -> bool:
        pass


@private
class ImplicitBinding(Binding):
    """A binding that was created implicitly by auto-binding."""

    pass


_InstallableModuleType = Union[Callable[['Binder'], None], 'Module', Type['Module']]


class Binder:
    """Bind interfaces to implementations.

    .. note:: This class is instantiated internally for you and there's no need
        to instantiate it on your own.
    """

    _bindings: Dict[type, Binding]

    @private
    def __init__(
        self, injector: 'Injector', auto_bind: bool = True, parent: Optional['Binder'] = None
    ) -> None:
        """Create a new Binder.

        :param injector: Injector we are binding for.
        :param auto_bind: Whether to automatically bind missing types.
        :param parent: Parent binder.
        """
        self.injector = injector
        self._auto_bind = auto_bind
        self._bindings = {}
        self.parent = parent

    def bind(
        self,
        interface: Type[T],
        to: Union[None, T, Callable[..., T], Provider[T]] = None,
        scope: Union[None, Type['Scope'], 'ScopeDecorator'] = None,
    ) -> None:
        """Bind an interface to an implementation.

        Binding `T` to an instance of `T` like

        ::

            binder.bind(A, to=A('some', 'thing'))

        is, for convenience, a shortcut for

        ::

            binder.bind(A, to=InstanceProvider(A('some', 'thing'))).

        Likewise, binding to a callable like

        ::

            binder.bind(A, to=some_callable)

        is a shortcut for

        ::

            binder.bind(A, to=CallableProvider(some_callable))

        and, as such, if `some_callable` there has any annotated parameters they'll be provided
        automatically without having to use :func:`inject` or :data:`Inject` with the callable.

        `typing.List` and `typing.Dict` instances are reserved for multibindings and trying to bind them
        here will result in an error (use :meth:`multibind` instead)::

            binder.bind(List[str], to=['hello', 'there'])  # Error

        :param interface: Type to bind.
        :param to: Instance or class to bind to, or an instance of
             :class:`Provider` subclass.
        :param scope: Optional :class:`Scope` in which to bind.
        """
        pass

    @overload
    def multibind(
        self,
        interface: Type[List[T]],
        to: Union[Collection[Union[T, Type[T]]], Callable[..., List[T]], Provider[List[T]], Type[T]],
        scope: Union[Type['Scope'], 'ScopeDecorator', None] = None,
    ) -> None:  # pragma: no cover
        pass

    @overload
    def multibind(
        self,
        interface: Type[Dict[K, V]],
        to: Union[Mapping[K, Union[V, Type[V]]], Callable[..., Dict[K, V]], Provider[Dict[K, V]]],
        scope: Union[Type['Scope'], 'ScopeDecorator', None] = None,
    ) -> None:  # pragma: no cover
        pass

    def multibind(
        self, interface: type, to: Any, scope: Union['ScopeDecorator', Type['Scope'], None] = None
    ) -> None:
        """Creates or extends a multi-binding.

        A multi-binding contributes values to a list or to a dictionary. For example::

            binder.multibind(list[Interface], to=A)
            binder.multibind(list[Interface], to=[B, C()])
            injector.get(list[Interface])
            # [<A object at 0x1000>, <B object at 0x2000>, <C object at 0x3000>]

            binder.multibind(dict[str, Interface], to={'key': A})
            binder.multibind(dict[str, Interface], to={'other_key': B})
            injector.get(dict[str, Interface])
            # {'key': <A object at 0x1000>, 'other_key': <B object at 0x2000>}

        .. versionchanged:: 0.17.0
            Added support for using `typing.Dict` and `typing.List` instances as interfaces.
            Deprecated support for `MappingKey`, `SequenceKey` and single-item lists and
            dictionaries as interfaces.

        :param interface: A generic list[T] or dict[str, T] type to bind to.

        :param to: A list/dict to bind to, where the values are either instances or classes implementing T.
                Can also be an explicit :class:`Provider` or a callable that returns a list/dict.
                For lists, this can also be a class implementing T (e.g. multibind(list[T], to=A))

        :param scope: Optional Scope in which to bind.
        """
        pass

    def _get_multi_binder(self, interface: type) -> MultiBinder:
        pass

    def install(self, module: _InstallableModuleType) -> None:
        """Install a module into this binder.

        In this context the module is one of the following:

        * function taking the :class:`Binder` as its only parameter

          ::

            def configure(binder):
                bind(str, to='s')

            binder.install(configure)

        * instance of :class:`Module` (instance of its subclass counts)

          ::

            class MyModule(Module):
                def configure(self, binder):
                    binder.bind(str, to='s')

            binder.install(MyModule())

        * subclass of :class:`Module` - the subclass needs to be instantiable so if it
          expects any parameters they need to be injected

          ::

            binder.install(MyModule)
        """
        pass

    def create_binding(
        self, interface: type, to: Any = None, scope: Union['ScopeDecorator', Type['Scope'], None] = None
    ) -> Binding:
        pass

    def provider_for(self, interface: Any, to: Any = None) -> Provider:
        pass

    def _get_binding(self, key: type, *, only_this_binder: bool = False) -> Tuple[Binding, 'Binder']:
        pass

    def get_binding(self, interface: type) -> Tuple[Binding, 'Binder']:
        pass

    def has_binding_for(self, interface: type) -> bool:
        pass

    def has_explicit_binding_for(self, interface: type) -> bool:
        pass

    def _is_special_interface(self, interface: type) -> bool:
        # "Special" interfaces are ones that you cannot bind yourself but
        # you can request them (for example you cannot bind ProviderOf(SomeClass)
        # to anything but you can inject ProviderOf(SomeClass) just fine
        pass


def _is_specialization(cls: type, generic_class: Any) -> bool:
    # Starting with typing 3.5.3/Python 3.6 it is no longer necessarily true that
    # issubclass(SomeGeneric[X], SomeGeneric) so we need some other way to
    # determine whether a particular object is a generic class with type parameters
    # provided. Fortunately there seems to be __origin__ attribute that's useful here.

    # We need to special-case Annotated as its __origin__ behaves differently than
    # other typing generic classes. See https://github.com/python/typing/pull/635
    # for some details.
    pass


def _ensure_iterable(item_or_list: Union[T, List[T]]) -> List[T]:
    pass


def _punch_through_alias(type_: Any) -> type:
    pass


def _get_origin(type_: type) -> Optional[type]:
    pass


class Scope:
    """A Scope looks up the Provider for a binding.

    By default (ie. :class:`NoScope` ) this simply returns the default
    :class:`Provider` .
    """

    __metaclass__ = ABCMeta

    def __init__(self, injector: 'Injector') -> None:
        self.injector = injector
        self.configure()

    def configure(self) -> None:
        """Configure the scope."""

    @abstractmethod
    def get(self, key: Type[T], provider: Provider[T]) -> Provider[T]:
        """Get a :class:`Provider` for a key.

        :param key: The key to return a provider for.
        :param provider: The default Provider associated with the key.
        :returns: A Provider instance that can provide an instance of key.
        """
        raise NotImplementedError  # pragma: no cover


class ScopeDecorator:
    def __init__(self, scope: Type[Scope]) -> None:
        self.scope = scope

    def __call__(self, cls: T) -> T:
        cast(Any, cls).__scope__ = self.scope
        binding = getattr(cls, '__binding__', None)
        if binding:
            new_binding = Binding(interface=binding.interface, provider=binding.provider, scope=self.scope)
            setattr(cls, '__binding__', new_binding)
        return cls

    def __repr__(self) -> str:
        return 'ScopeDecorator(%s)' % self.scope.__name__


class NoScope(Scope):
    """An unscoped provider."""

    def get(self, key: Type[T], provider: Provider[T]) -> Provider[T]:
        pass


noscope = ScopeDecorator(NoScope)


class SingletonScope(Scope):
    """A :class:`Scope` that returns a per-Injector instance for a key.

    :data:`singleton` can be used as a convenience class decorator.

    >>> class A: pass
    >>> injector = Injector()
    >>> provider = ClassProvider(A)
    >>> singleton = SingletonScope(injector)
    >>> a = singleton.get(A, provider)
    >>> b = singleton.get(A, provider)
    >>> a is b
    True
    """

    _context: Dict[type, Provider]

    def configure(self) -> None:
        pass

    @synchronized(lock)
    def get(self, key: Type[T], provider: Provider[T]) -> Provider[T]:
        pass

    def _get_instance(self, key: Type[T], provider: Provider[T], injector: 'Injector') -> T:
        pass

    def _get_instance_from_parent(self, key: Type[T], provider: Provider[T], parent: 'Injector') -> T:
        pass


singleton = ScopeDecorator(SingletonScope)


class ThreadLocalScope(Scope):
    """A :class:`Scope` that returns a per-thread instance for a key."""

    def configure(self) -> None:
        pass

    def get(self, key: Type[T], provider: Provider[T]) -> Provider[T]:
        pass


threadlocal = ScopeDecorator(ThreadLocalScope)


class Module:
    """Configures injector and providers."""

    def __call__(self, binder: Binder) -> None:
        """Configure the binder."""
        self.__injector__ = binder.injector
        for unused_name, function in inspect.getmembers(self, inspect.ismethod):
            binding = None
            if hasattr(function, '__binding__'):
                binding = function.__binding__
                if binding.interface == '__deferred__':
                    # We could not evaluate a forward reference at @provider-decoration time, we need to
                    # try again now.
                    try:
                        annotations = get_type_hints(function)
                    except NameError as e:
                        raise NameError(
                            'Cannot avaluate forward reference annotation(s) in method %r belonging to %r: %s'
                            % (function.__name__, type(self), e)
                        ) from e
                    return_type = annotations['return']
                    binding = cast(Any, function.__func__).__binding__ = Binding(
                        interface=return_type, provider=binding.provider, scope=binding.scope
                    )
                bind_method = binder.multibind if binding.is_multibinding() else binder.bind
                bind_method(  # type: ignore
                    binding.interface, to=types.MethodType(binding.provider, self), scope=binding.scope
                )
        self.configure(binder)

    def configure(self, binder: Binder) -> None:
        """Override to configure bindings."""


class Injector:
    """
    :param modules: Optional - a configuration module or iterable of configuration modules.
        Each module will be installed in current :class:`Binder` using :meth:`Binder.install`.

        Consult :meth:`Binder.install` documentation for the details.

    :param auto_bind: Whether to automatically bind missing types.
    :param parent: Parent injector.

    .. versionadded:: 0.7.5
        ``use_annotations`` parameter

    .. versionchanged:: 0.13.0
        ``use_annotations`` parameter is removed
    """

    _stack: Tuple[Tuple[object, Callable, Tuple[Tuple[str, type], ...]], ...]
    binder: Binder

    def __init__(
        self,
        modules: Union[_InstallableModuleType, Iterable[_InstallableModuleType], None] = None,
        auto_bind: bool = True,
        parent: Optional['Injector'] = None,
    ) -> None:
        # Stack of keys currently being injected. Used to detect circular
        # dependencies.
        self._stack = ()

        self.parent = parent

        # Binder
        self.binder = Binder(self, auto_bind=auto_bind, parent=parent.binder if parent is not None else None)

        if not modules:
            modules = []
        elif not hasattr(modules, '__iter__'):
            modules = [cast(_InstallableModuleType, modules)]
        # This line is needed to pelase mypy. We know we have Iteable of modules here.
        modules = cast(Iterable[_InstallableModuleType], modules)

        # Bind some useful types
        self.binder.bind(Injector, to=self)
        self.binder.bind(Binder, to=self.binder)

        # Initialise modules
        for module in modules:
            self.binder.install(module)

    @property
    def _log_prefix(self) -> str:
        pass

    @synchronized(lock)
    def get(self, interface: Type[T], scope: Union[ScopeDecorator, Type[Scope], None] = None) -> T:
        """Get an instance of the given interface.

        .. note::

            Although this method is part of :class:`Injector`'s public interface
            it's meant to be used in limited set of circumstances.

            For example, to create some kind of root object (application object)
            of your application (note that only one `get` call is needed,
            inside the `Application` class and any of its dependencies
            :func:`inject` can and should be used):

            .. code-block:: python

                class Application:

                    @inject
                    def __init__(self, dep1: Dep1, dep2: Dep2):
                        self.dep1 = dep1
                        self.dep2 = dep2

                    def run(self):
                        self.dep1.something()

                injector = Injector(configuration)
                application = injector.get(Application)
                application.run()

        :param interface: Interface whose implementation we want.
        :param scope: Class of the Scope in which to resolve.
        :returns: An implementation of interface.
        """
        pass

    def create_child_injector(self, *args: Any, **kwargs: Any) -> 'Injector':
        pass

    def create_object(self, cls: Type[T], additional_kwargs: Any = None) -> T:
        """Create a new instance, satisfying any dependencies on cls."""
        pass

    def call_with_injection(
        self, callable: Callable[..., T], self_: Any = None, args: Any = (), kwargs: Any = {}
    ) -> T:
        """Call a callable and provide its dependencies if needed.

        Dependencies are provided when the callable is decorated with :func:`@inject <inject>`
        or some individual parameters are wrapped in :data:`Inject` – otherwise
        ``call_with_injection()`` is equivalent to just calling the callable directly.

        If there is an overlap between arguments provided in ``args`` and ``kwargs``
        and injectable dependencies the provided values take precedence and no dependency
        injection process will take place for the corresponding parameters.

        :param self_: Instance of a class callable belongs to if it's a method,
            None otherwise.
        :param args: Arguments to pass to callable.
        :param kwargs: Keyword arguments to pass to callable.
        :type callable: callable
        :type args: tuple of objects
        :type kwargs: dict of string -> object
        :return: Value returned by callable.
        """
        pass

    @private
    @synchronized(lock)
    def args_to_inject(
        self, function: Callable, bindings: Dict[str, type], owner_key: object
    ) -> Dict[str, Any]:
        """Inject arguments into a function.

        :param function: The function.
        :param bindings: Map of argument name to binding key to inject.
        :param owner_key: A key uniquely identifying the *scope* of this function.
            For a method this will be the owning class.
        :returns: Dictionary of resolved arguments.
        """
        pass


def get_bindings(callable: Callable) -> Dict[str, type]:
    """Get bindings of injectable parameters from a callable.

    If the callable is not decorated with :func:`inject` and does not have any of its
    parameters declared as injectable using :data:`Inject` an empty dictionary will
    be returned.  Otherwise the returned dictionary will contain a mapping
    between parameter names and their types with the exception of parameters
    excluded from dependency injection (either with :func:`noninjectable`, :data:`NoInject`
    or only explicit injection with :data:`Inject` being used). For example::

        >>> def function1(a: int) -> None:
        ...     pass
        ...
        >>> get_bindings(function1)
        {}

        >>> @inject
        ... def function2(a: int) -> None:
        ...     pass
        ...
        >>> get_bindings(function2)
        {'a': <class 'int'>}

        >>> @inject
        ... @noninjectable('b')
        ... def function3(a: int, b: str) -> None:
        ...     pass
        ...
        >>> get_bindings(function3)
        {'a': <class 'int'>}

        >>> # The simple case of no @inject but injection requested with Inject[...]
        >>> def function4(a: Inject[int], b: str) -> None:
        ...     pass
        ...
        >>> get_bindings(function4)
        {'a': <class 'int'>}

        >>> # Using @inject with Inject is redundant but it should not break anything
        >>> @inject
        ... def function5(a: Inject[int], b: str) -> None:
        ...     pass
        ...
        >>> get_bindings(function5)
        {'a': <class 'int'>, 'b': <class 'str'>}

        >>> # We need to be able to exclude a parameter from injection with NoInject
        >>> @inject
        ... def function6(a: int, b: NoInject[str]) -> None:
        ...     pass
        ...
        >>> get_bindings(function6)
        {'a': <class 'int'>}

        >>> # The presence of NoInject should not trigger anything on its own
        >>> def function7(a: int, b: NoInject[str]) -> None:
        ...     pass
        ...
        >>> get_bindings(function7)
        {}

    This function is used internally so by calling it you can learn what exactly
    Injector is going to try to provide to a callable.
    """
    pass


class _BindingNotYetAvailable(Exception):
    pass


# See a comment in _infer_injected_bindings() for why this is useful.
class _NoReturnAnnotationProxy:
    def __init__(self, callable: Callable) -> None:
        self.callable = callable

    def __getattribute__(self, name: str) -> Any:
        # get_type_hints() uses quite complex logic to determine the namespaces using which
        # any forward references should be resolved. Instead of mirroring this logic here
        # let's just take the easy way out and forward all attribute access to the original
        # callable except for the annotations – we want to filter them.
        callable = object.__getattribute__(self, 'callable')
        if name == '__annotations__':
            annotations = callable.__annotations__
            return {name: value for (name, value) in annotations.items() if name != 'return'}
        return getattr(callable, name)


def _infer_injected_bindings(callable: Callable, only_explicit_bindings: bool) -> Dict[str, type]:
    pass


def provider(function: CallableT) -> CallableT:
    """Decorator for :class:`Module` methods, registering a provider of a type.

    >>> class MyModule(Module):
    ...   @provider
    ...   def provide_name(self) -> str:
    ...       return 'Bob'

    @provider-decoration implies @inject so you can omit it and things will
    work just the same:

    >>> class MyModule2(Module):
    ...     def configure(self, binder):
    ...         binder.bind(int, to=654)
    ...
    ...     @provider
    ...     def provide_str(self, i: int) -> str:
    ...         return str(i)
    ...
    >>> injector = Injector(MyModule2)
    >>> injector.get(str)
    '654'
    """
    pass


def multiprovider(function: CallableT) -> CallableT:
    """Like :func:`provider`, but for multibindings. Example usage::

        class MyModule(Module):
            @multiprovider
            def provide_strs(self) -> List[str]:
                return ['str1']

        class OtherModule(Module):
            @multiprovider
            def provide_strs_also(self) -> List[str]:
                return ['str2']

        Injector([MyModule, OtherModule]).get(List[str])  # ['str1', 'str2']

    See also: :meth:`Binder.multibind`."""
    pass


def _mark_provider_function(function: Callable, *, allow_multi: bool) -> None:
    pass


def _validate_provider_return_type(function: Callable, return_type: type, allow_multi: bool) -> None:
    pass


ConstructorOrClassT = TypeVar('ConstructorOrClassT', bound=Union[Callable, Type])


@overload
def inject(constructor_or_class: CallableT) -> CallableT:  # pragma: no cover
    pass


@overload
def inject(constructor_or_class: Type[T]) -> Type[T]:  # pragma: no cover
    pass


def inject(constructor_or_class: ConstructorOrClassT) -> ConstructorOrClassT:
    """Decorator declaring parameters to be injected.

    eg.

    >>> class A:
    ...     @inject
    ...     def __init__(self, number: int, name: str):
    ...         print([number, name])
    ...
    >>> def configure(binder):
    ...     binder.bind(A)
    ...     binder.bind(int, to=123)
    ...     binder.bind(str, to='Bob')

    Use the Injector to get a new instance of A:

    >>> a = Injector(configure).get(A)
    [123, 'Bob']

    As a convenience one can decorate a class itself::

        @inject
        class B:
            def __init__(self, dependency: Dependency):
                self.dependency = dependency

    This is equivalent to decorating its constructor. In particular this provides integration with
    `dataclasses <https://docs.python.org/3/library/dataclasses.html>`_ (the order of decorator
    application is important here)::

        @inject
        @dataclass
        class C:
            dependency: Dependency

    .. note::

        This decorator is to be used on class constructors (or, as a convenience, on classes).
        Using it on non-constructor methods worked in the past but it was an implementation
        detail rather than a design decision.

        Third party libraries may, however, provide support for injecting dependencies
        into non-constructor methods or free functions in one form or another.

    .. seealso::

        Generic type :data:`Inject`
            A more explicit way to declare parameters as injectable.

        Function :func:`get_bindings`
            A way to inspect how various injection declarations interact with each other.

    .. versionchanged:: 0.16.2

        (Re)added support for decorating classes with @inject.
    """
    pass


def noninjectable(*args: str) -> Callable[[CallableT], CallableT]:
    """Mark some parameters as not injectable.

    This serves as documentation for people reading the code and will prevent
    Injector from ever attempting to provide the parameters.

    For example:

    >>> class Service:
    ...    pass
    ...
    >>> class SomeClass:
    ...     @inject
    ...     @noninjectable('user_id')
    ...     def __init__(self, service: Service, user_id: int):
    ...         # ...
    ...         pass

    :func:`noninjectable` decorations can be stacked on top of
    each other and the order in which a function is decorated with
    :func:`inject` and :func:`noninjectable`
    doesn't matter.

    .. seealso::

        Generic type :data:`NoInject`
            A nicer way to declare parameters as noninjectable.

        Function :func:`get_bindings`
            A way to inspect how various injection declarations interact with each other.

    """
    pass


@private
def read_and_store_bindings(f: Callable, bindings: Dict[str, type]) -> None:
    pass


class BoundKey(tuple):
    """A BoundKey provides a key to a type with pre-injected arguments.

    >>> class A:
    ...   def __init__(self, a, b):
    ...     self.a = a
    ...     self.b = b
    >>> InjectedA = BoundKey(A, a=InstanceProvider(1), b=InstanceProvider(2))
    >>> injector = Injector()
    >>> a = injector.get(InjectedA)
    >>> a.a, a.b
    (1, 2)
    """

    def __new__(cls, interface: Type[T], **kwargs: Any) -> 'BoundKey':
        kwargs_tuple = tuple(sorted(kwargs.items()))
        return super(BoundKey, cls).__new__(cls, (interface, kwargs_tuple))  # type: ignore

    @property
    def interface(self) -> Type[T]:
        pass

    @property
    def kwargs(self) -> Dict[str, Any]:
        pass


class AssistedBuilder(Generic[T]):
    def __init__(self, injector: Injector, target: Type[T]) -> None:
        self._injector = injector
        self._target = target

    def build(self, **kwargs: Any) -> T:
        pass

    def _build_class(self, cls: Type[T], **kwargs: Any) -> T:
        pass


class ClassAssistedBuilder(AssistedBuilder[T]):
    def build(self, **kwargs: Any) -> T:
        pass


def _describe(c: Any) -> str:
    pass


class ProviderOf(Generic[T]):
    """Can be used to get a provider of an interface, for example:

    >>> def provide_int():
    ...     print('providing')
    ...     return 123
    >>>
    >>> def configure(binder):
    ...     binder.bind(int, to=provide_int)
    >>>
    >>> injector = Injector(configure)
    >>> provider = injector.get(ProviderOf[int])
    >>> value = provider.get()
    providing
    >>> value
    123
    """

    def __init__(self, injector: Injector, interface: Type[T]):
        self._injector = injector
        self._interface = interface

    def __repr__(self) -> str:
        return '%s(%r, %r)' % (type(self).__name__, self._injector, self._interface)

    def get(self) -> T:
        """Get an implementation for the specified interface."""
        pass


def is_decorated_with_inject(function: Callable[..., Any]) -> bool:
    """See if given callable is declared to want some dependencies injected.

    Example use:

    >>> def fun(i: int) -> str:
    ...     return str(i)

    >>> is_decorated_with_inject(fun)
    False
    >>>
    >>> @inject
    ... def fun2(i: int) -> str:
    ...     return str(i)

    >>> is_decorated_with_inject(fun2)
    True
    """
    pass
