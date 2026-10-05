variable "resource_group_name" {
  type        = string
  description = "Azure Resource Group name"
  default     = "rg-supportdesk-prod"
}

variable "location" {
  type        = string
  description = "Azure Region"
  default     = "southeastasia"
}

variable "environment" {
  type        = string
  description = "Deployment environment"
  default     = "production"
}

variable "acr_server" {
  type        = string
  description = "Azure Container Registry login server"
  default     = "crportfolioprod.azurecr.io"
}

variable "image_tag" {
  type        = string
  description = "Container image tag"
  default     = "latest"
}
