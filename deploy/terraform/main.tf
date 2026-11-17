terraform {
  required_version = ">= 1.6, < 2.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "region" {
  description = "Region containing the approved lab subnet and key pair."
  type        = string
  default     = "eu-north-1"
}

variable "subnet_id" {
  description = "Existing IPv4 public subnet approved for the staging EC2 instance."
  type        = string
}

variable "key_name" {
  description = "Existing EC2 key pair name. Supply the corresponding private key locally to SSH."
  type        = string
}

variable "ssh_cidr" {
  description = "One authorised operator IPv4 address in /32 notation."
  type        = string

  validation {
    condition     = can(cidrnetmask(var.ssh_cidr)) && endswith(var.ssh_cidr, "/32")
    error_message = "Supply exactly one authorised IPv4 address as a /32 CIDR."
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "StockholmWeatherTimeCapsule"
      Environment = "staging"
    }
  }
}

data "aws_subnet" "selected" {
  id = var.subnet_id
}

data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

resource "aws_security_group" "capsule" {
  name_prefix = "weather-capsule-staging-"
  description = "Restrict staging administration to one SSH address"
  vpc_id      = data.aws_subnet.selected.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  security_group_id = aws_security_group.capsule.id
  description       = "SSH administration and encrypted preview tunnel"
  cidr_ipv4         = var.ssh_cidr
  ip_protocol       = "tcp"
  from_port         = 22
  to_port           = 22
}

resource "aws_vpc_security_group_egress_rule" "https" {
  security_group_id = aws_security_group.capsule.id
  description       = "HTTPS updates, dependency installation and Bedrock"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_iam_role" "capsule" {
  name_prefix = "weather-capsule-staging-"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "bedrock" {
  name = "invoke-stockholm-capsule-models"
  role = aws_iam_role.capsule.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "bedrock:InvokeModel"
      Resource = [
        "arn:aws:bedrock:eu-north-1::foundation-model/amazon.nova-lite-v1:0",
        "arn:aws:bedrock:eu-north-1::foundation-model/qwen.qwen3-235b-a22b-2507-v1:0",
      ]
    }]
  })
}

resource "aws_iam_instance_profile" "capsule" {
  name_prefix = "weather-capsule-staging-"
  role        = aws_iam_role.capsule.name
}

resource "aws_instance" "capsule" {
  ami                         = nonsensitive(data.aws_ssm_parameter.al2023.value)
  instance_type               = "t3.micro"
  subnet_id                   = data.aws_subnet.selected.id
  key_name                    = var.key_name
  vpc_security_group_ids      = [aws_security_group.capsule.id]
  associate_public_ip_address = true
  iam_instance_profile        = aws_iam_instance_profile.capsule.name

  credit_specification {
    cpu_credits = "standard"
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    encrypted             = true
    volume_type           = "gp3"
    volume_size           = 8
    delete_on_termination = true
  }

  tags = { Name = "weather-capsule-staging" }
}

output "ssh_host" {
  description = "Public IPv4 address for the restricted staging SSH connection."
  value       = aws_instance.capsule.public_ip
}
