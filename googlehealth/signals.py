"""Django signals emitted by googlehealth views.

``notification_received`` fires for every authenticated, non-verification POST
to the webhook receiver view. Connect a handler to drive ingest:

.. code-block:: python

    from django.dispatch import receiver
    from googlehealth.signals import notification_received
    from googlehealth.webhooks import process_notification

    @receiver(notification_received)
    def on_notification(sender, payload, **kwargs):
        process_notification(payload)  # or hand off to celery / etc.

Sender is ``None`` (signal is namespace-only). The ``payload`` keyword carries
the parsed JSON body exactly as Google sent it.

``mobile_connected`` fires from :func:`googlehealth.views.mobile_callback`
after a successful token exchange + ingest, with ``customer`` and
``connection`` keywords. Use it to flip project-side state (activate the data
source for the user, enqueue a first sync, …). Receivers run synchronously
before the app is deep-linked; a receiver that raises turns the app's result
into ``status=error``.
"""

import django.dispatch

notification_received = django.dispatch.Signal()
mobile_connected = django.dispatch.Signal()
