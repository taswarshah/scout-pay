# Optional: custom domain (e.g. pay.taswar.dev) for the scout-pay Worker.
# Apply only AFTER `wrangler deploy` has created the Worker.
terraform {
  required_providers {
    cloudflare = { source = "cloudflare/cloudflare", version = "~> 5.0" }
  }
}

provider "cloudflare" {} # reads CLOUDFLARE_API_TOKEN from env

variable "account_id" { type = string }
variable "zone_id" {
  type    = string
  default = ""
  description = "Zone ID of taswar.dev; leave empty to skip the custom domain"
}
variable "hostname" {
  type    = string
  default = "pay.taswar.dev"
}
variable "worker_name" {
  type    = string
  default = "scout-pay"
}

resource "cloudflare_workers_custom_domain" "scout_pay" {
  count       = var.zone_id == "" ? 0 : 1
  account_id  = var.account_id
  zone_id     = var.zone_id
  hostname    = var.hostname
  service     = var.worker_name
  environment = "production"
}

# Persistence (planned): D1 database for transactions/approvals audit log.
# resource "cloudflare_d1_database" "scout_pay" {
#   account_id = var.account_id
#   name       = "scout-pay"
# }

output "url" {
  value = var.zone_id == "" ? "use the workers.dev URL printed by wrangler" : "https://${var.hostname}"
}
