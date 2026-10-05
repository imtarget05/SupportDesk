resource "azurerm_container_app" "api" {
  name                         = "supportdesk-api"
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = azurerm_resource_group.rg.name
  revision_mode                = "Single"
  tags                         = azurerm_resource_group.rg.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.identity.id]
  }

  template {
    min_replicas = 0
    max_replicas = 5

    container {
      name   = "supportdesk-api"
      image  = "${var.acr_server}/supportdesk-api:${var.image_tag}"
      cpu    = 0.5
      memory = "1.0Gi"

      env {
        name  = "ENVIRONMENT"
        value = "production"
      }
      env {
        name  = "PORT"
        value = "8000"
      }
      env {
        name  = "SERVICE_BUS_NAMESPACE"
        value = azurerm_servicebus_namespace.sb.name
      }
      env {
        name  = "KEY_VAULT_URI"
        value = azurerm_key_vault.kv.vault_uri
      }
    }

    http_scale_rule {
      name                = "http-rule"
      concurrent_requests = 50
    }
  }

  ingress {
    external_enabled = true
    target_port      = 8000
    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }
}

resource "azurerm_container_app" "outbox_worker" {
  name                         = "supportdesk-outbox-worker"
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = azurerm_resource_group.rg.name
  revision_mode                = "Single"
  tags                         = azurerm_resource_group.rg.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.identity.id]
  }

  template {
    min_replicas = 0
    max_replicas = 2

    container {
      name    = "outbox-worker"
      image   = "${var.acr_server}/supportdesk-api:${var.image_tag}"
      command = ["python", "-m", "app.workers.outbox_publisher"]
      cpu     = 0.25
      memory  = "0.5Gi"

      env {
        name  = "SERVICE_BUS_NAMESPACE"
        value = azurerm_servicebus_namespace.sb.name
      }
    }
  }
}

resource "azurerm_container_app" "notification_worker" {
  name                         = "supportdesk-notification-worker"
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = azurerm_resource_group.rg.name
  revision_mode                = "Single"
  tags                         = azurerm_resource_group.rg.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.identity.id]
  }

  template {
    min_replicas = 0
    max_replicas = 5

    container {
      name    = "notification-worker"
      image   = "${var.acr_server}/supportdesk-api:${var.image_tag}"
      command = ["python", "-m", "app.workers.notification_worker"]
      cpu     = 0.25
      memory  = "0.5Gi"

      env {
        name  = "SERVICE_BUS_NAMESPACE"
        value = azurerm_servicebus_namespace.sb.name
      }
      env {
        name  = "TOPIC_NAME"
        value = azurerm_servicebus_topic.ticket_events.name
      }
      env {
        name  = "SUBSCRIPTION_NAME"
        value = azurerm_servicebus_subscription.notification_sub.name
      }
    }
  }
}
