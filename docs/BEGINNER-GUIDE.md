# Stockholm Weather Time Capsule beginner’s guide

Start with **Open the demo** and **Pause the demo** for day-to-day use. Use **Install or update the app** when you need to deploy a change. The [deployment runbook](../DEPLOYMENT.md) has the full setup, access and teardown procedures.

## Know which terminal you’re in

| Where you are | What you do there |
| --- | --- |
| **Ubuntu in WSL**, on your computer | Work with Git, run Terraform and Ansible, and start SSH. |
| **EC2 over SSH**, at an `ec2-user@…` prompt | Start or pause Funnel and check the running app. Type `exit` to return to Ubuntu. |
| **Browser** | Use the HTTPS demo link, the AWS console, GitHub or the Tailscale admin console. |

- `pwd` shows your current folder. `ls` lists its files. `cd` changes folder. `cd ..` goes up one level.
- **Up Arrow** recalls the last command. Edit it before pressing **Enter**. **Ctrl+R** searches command history. `history 15` shows the last 15 commands.
- **Ctrl+C** cancels the current command. A **`>`** prompt usually means Bash is waiting for the rest of a command, such as a missing quote or a line continued with `\`. Press **Ctrl+C** if you want to start that command again.
- In **Nano**, use **Ctrl+O**, then **Enter** to save, and **Ctrl+X** to exit.

From Ubuntu, use this command to return to the project from any folder.

```bash
cd /path/to/stockholm-weather-time-capsule-public
```

## Open the demo

1. In **AWS EC2 → Instances**, select `weather-capsule-staging` in **Europe (Stockholm)**. If it’s stopped, choose **Start instance** and wait for it to run. Copy its current **Public IPv4 address**. AWS can assign a new public address after a stop and start.
2. With your VPN connected, run `curl -4fsS https://checkip.amazonaws.com` in Ubuntu. If this address changed, edit `deploy/terraform/terraform.tfvars` with Nano and update `ssh_cidr` to the VPN address followed by `/32`. Follow the Terraform plan and review steps below to update the SSH rule. The EC2 address is where you connect. The VPN address is where the SSH connection comes from.
3. In Ubuntu, set the two variables below in the current terminal. Replace the placeholder with the **EC2** address from step 1. The key path is the one used for this staging host. Variables set this way last for the current terminal session.

```bash
CAPSULE_HOST='PASTE_CURRENT_EC2_PUBLIC_IPV4_HERE'
CAPSULE_KEY="$HOME/.ssh/stockholm-capsule-staging.pem"

ls -l "$CAPSULE_KEY" &&
ssh -o IdentitiesOnly=yes -i "$CAPSULE_KEY" "ec2-user@$CAPSULE_HOST"
```

If SSH asks about a new fingerprint, verify it before accepting. For the same EC2 instance at a new address, check the instance ID in AWS and compare the fingerprint with the key saved under its previous address. Run the following in Ubuntu, replacing the placeholder with the previously verified EC2 address.

```bash
ssh-keygen -F PREVIOUS_EC2_PUBLIC_IPV4 |
  awk '$2 ~ /^(ssh-|ecdsa-)/ {print $2, $3}' |
  ssh-keygen -lf -
```

Compare the full SHA256 fingerprint and key type. If the lookup is empty or the key differs, leave the new SSH prompt unaccepted while you verify the host.

4. At the **`ec2-user@…`** prompt, run the Funnel commands below. The status command prints the HTTPS demo address. Open it in a private browser window and check that the login prompt appears and your demo credentials open the app.

```bash
sudo tailscale funnel --bg --https=443 http://127.0.0.1:8081
sudo tailscale funnel status
```

**Find date** looks up the weather and any saved dispatch. **Create time capsule** makes a new Bedrock request. A page load and login check use no generation attempt. Colleagues can use the HTTPS link while EC2 and Funnel are running, including while your computer is offline.

For the original private preview, run the SSH tunnel below from Ubuntu and leave that terminal open. Visit [the private preview](http://127.0.0.1:8768/). Press **Ctrl+C** in the tunnel terminal to close your preview connection.

```bash
ssh -N -o ExitOnForwardFailure=yes -o IdentitiesOnly=yes \
  -L 127.0.0.1:8768:127.0.0.1:8080 \
  -i "$CAPSULE_KEY" "ec2-user@$CAPSULE_HOST"
```

## Pause the demo

1. In the **EC2 SSH shell**, run `sudo tailscale funnel reset`. Check the HTTPS link from a private browser window. It should be unavailable.
2. Type `exit` to return to Ubuntu. To pause EC2 compute usage, choose **Stop instance** in AWS. This preserves the app and demo logins on the EBS disk. The in-memory dispatch cache is cleared when the app restarts.
3. After the final demo, follow the [full shutdown procedure](../DEPLOYMENT.md#open-the-protected-demo-link) to remove the demo logins. Use the Terraform cleanup procedure in the runbook when the staging resources can be removed.

## Install or update the app

### 1. Check out the code you intend to install

From the project root in Ubuntu, run `git status --short --branch` and `git log -1 --oneline`. Once the release changes have been merged, update your local copy of `main` with these commands. The line-ending setting accounts for the shared Windows and Ubuntu checkout.

```bash
git -c core.autocrlf=true switch main &&
git -c core.autocrlf=true pull --ff-only origin main
```

### 2. Use Terraform when AWS resources need a change

For initial setup, fill in `deploy/terraform/terraform.tfvars` with the public `subnet_id`, AWS `key_name` and VPN `ssh_cidr`. This project uses Stockholm by default. Run the commands below from the project root in Ubuntu.

```bash
terraform -chdir=deploy/terraform init &&
terraform -chdir=deploy/terraform validate &&
terraform -chdir=deploy/terraform plan -out=staging-reviewed.tfplan &&
terraform -chdir=deploy/terraform show staging-reviewed.tfplan
```

**Plan** previews the changes. **Show** displays the saved plan. Use `terraform -chdir=deploy/terraform apply staging-reviewed.tfplan` after checking that the plan contains the changes you intend. A VPN change should affect the staging SSH rule. If Terraform reports **No changes**, you can proceed with SSH. For an app-only update, continue to Ansible.

### 3. Use Ansible to install the app on EC2

Check `ansible-playbook --version` in Ubuntu. Before enabling demo access for the first time, create the demo logins on EC2 and set up Tailscale as described in the [demo deployment procedure](../DEPLOYMENT.md#open-the-protected-demo-link). From the project root in Ubuntu, use the host and key variables from **Open the demo** and run the playbook.

```bash
ansible-playbook -i "$CAPSULE_HOST," -u ec2-user --private-key "$CAPSULE_KEY" \
  -e capsule_dedicated_host=true \
  -e capsule_bedrock=true \
  -e capsule_bedrock_model=qwen \
  -e capsule_demo_access=true \
  deploy/install.yml
```

The comma after the host tells Ansible to use a one-host inventory. Check **PLAY RECAP** for `unreachable=0` and `failed=0`. The ten-attempt option uses `-e capsule_dispatch_limit=10` with the same command once you deploy the version that supports it. The default is five. Recheck the HTTPS login after an update.

## Find the cause of a common error

| What you see | What to check |
| --- | --- |
| `Identity file  not accessible` with a blank filename | Set `CAPSULE_KEY` in this Ubuntu terminal, then run `ls -l "$CAPSULE_KEY"`. |
| SSH times out | Check that EC2 is running, the public address is current and the inbound port 22 rule matches today’s VPN IPv4 with `/32`. |
| `deploy/install.yml could not be found` | Run `pwd`, return to the project root and check `ls deploy/install.yml`. |
| Git says local changes would be overwritten | Check `git status` and inspect the changes. Use the line-ending setting shown above for this shared checkout. |
| The app reports the hourly limit | You can still use **Find date** and view saved dispatches. A slot opens when an earlier attempt is 60 minutes old. |
