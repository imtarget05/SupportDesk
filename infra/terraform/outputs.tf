output "container_app_fqdn" {
  description = "FQDN of the SupportDesk API Container App"
  value       = azurerm_container_app.api.latest_revision_fqdn
}

output "service_bus_namespace" {
  description = "Azure Service Bus Namespace"
  value       = azurerm_servicebus_namespace.sb.name
}

output "key_vault_uri" {
  description = "Azure Key Vault URI"
  value       = azurerm_key_vault.kv.vault_uri
}
