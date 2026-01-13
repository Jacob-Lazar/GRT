API Reference
=============

.. meta::
   :description: FL-GRT API Reference - Gate SDK and Reflect Collector.

This section provides auto-generated API documentation for the FL-GRT components.

Gate SDK (``gate``)
-------------------

The Gate SDK is the core instrumentation library that runs in-process with your agent.

Core Module
~~~~~~~~~~~

.. automodule:: gate.core
   :members:
   :undoc-members:
   :show-inheritance:

Decorators & Context Managers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. autofunction:: gate.observe

.. autofunction:: gate.step

.. autofunction:: gate.thought

.. autofunction:: gate.guard

.. autofunction:: gate.branch

.. autofunction:: gate.snapshot

.. autofunction:: gate.event

Configuration
~~~~~~~~~~~~~

.. automodule:: gate.config
   :members:
   :undoc-members:

Transport Layer
~~~~~~~~~~~~~~~

.. automodule:: gate.transport
   :members:
   :undoc-members:

Reflect Collector (``reflect``)
-------------------------------

The Reflect Collector is the ingestion service that receives telemetry from Gate.

Main Application
~~~~~~~~~~~~~~~~

.. automodule:: reflect.main
   :members:
   :undoc-members:

Authentication
~~~~~~~~~~~~~~

.. automodule:: reflect.auth
   :members:
   :undoc-members:

Webhook (Low-Code Integration)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. automodule:: reflect.webhook
   :members:
   :undoc-members:

Metrics
~~~~~~~

.. automodule:: reflect.metrics
   :members:
   :undoc-members:

Storage
~~~~~~~

.. automodule:: reflect.storage
   :members:
   :undoc-members:
