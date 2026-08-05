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
source for the user, enqueue a first sync, …).

Two things to know about when receivers run:

* **Synchronously, inside the browser-facing callback.** The user's browser is
  blocked on this request, inside an in-app auth session. Do DB work here and
  hand anything network-bound (calling another vendor's API, unsubscribing a
  previous wearable) to a queue — otherwise a slow dependency becomes a gateway
  timeout with no deep link back to the app.
* **Inside the callback's transaction.** A receiver that raises rolls back the
  ``GoogleHealthConnection`` and sends ``status=error&reason=activation_failed``
  to the app, so "error" always means nothing was persisted. Receivers are
  therefore a veto on the connection: raise only if it genuinely shouldn't stand.
"""

import django.dispatch

notification_received = django.dispatch.Signal()
mobile_connected = django.dispatch.Signal()
