"""Re-export: the broker abstraction lives in the shared ``nvb_platform`` library."""

from nvb_platform.broker import (  # noqa: F401
    DEAD_LETTER_EXCHANGE,
    DELIVERY_LIMIT,
    Broker,
    BrokerUnavailable,
    Delivery,
    InMemoryBroker,
    PublishError,
    RabbitBroker,
)
