# nonprod root module. Replace the demo resource below with your real configuration.
#
# The pipeline runs `terraform init / fmt / validate / plan` from this directory on every PR
# and `plan -> (gate) -> apply` from here after a merge to main.

terraform {
  required_version = ">= 1.9"

  # Remote state (uncomment and adjust). Keep one state key per environment.
  # backend "s3" {
  #   bucket       = "acme-terraform-state"
  #   key          = "nonprod/terraform.tfstate"
  #   region       = "us-east-1"
  #   use_lockfile = true
  # }

  required_providers {
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

# Demo resource so the pipeline is runnable before any cloud credentials exist.
resource "random_pet" "demo" {
  length = 3

  keepers = {
    environment = "nonprod"
  }
}

output "demo_name" {
  value = random_pet.demo.id
}
