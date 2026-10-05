resource "azurerm_servicebus_namespace" "sb" {
  name                = "supportdesk-sb-${random_string.suffix.result}"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  sku                 = "Standard"
  tags                = azurerm_resource_group.rg.tags
}

resource "azurerm_servicebus_topic" "ticket_events" {
  name         = "ticket-events"
  namespace_id = azurerm_servicebus_namespace.sb.id

  partitioning_enabled                 = false
  requires_duplicate_detection         = true
  duplicate_detection_history_time_window = "PT10M"
  default_message_ttl                  = "P14D"
}

resource "azurerm_servicebus_subscription" "notification_sub" {
  name               = "notification-worker-sub"
  topic_id           = azurerm_servicebus_topic.ticket_events.id
  max_delivery_count = 3

  dead_lettering_on_message_expiration = true
  dead_lettering_on_filter_evaluation_error = true
}

resource "azurerm_servicebus_subscription" "automation_sub" {
  name               = "automation-worker-sub"
  topic_id           = azurerm_servicebus_topic.ticket_events.id
  max_delivery_count = 3

  dead_lettering_on_message_expiration = true
}

resource "azurerm_servicebus_subscription" "audit_sub" {
  name               = "audit-worker-sub"
  topic_id           = azurerm_servicebus_topic.ticket_events.id
  max_delivery_count = 5
}

resource "azurerm_servicebus_queue" "automation_jobs" {
  name         = "automation-jobs"
  namespace_id = azurerm_servicebus_namespace.sb.id

  requires_duplicate_detection         = true
  duplicate_detection_history_time_window = "PT10M"
  max_delivery_count                   = 3
  dead_lettering_on_message_expiration = true
}
