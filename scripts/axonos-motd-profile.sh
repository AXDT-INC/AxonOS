# Print the AxonOS banner for interactive login shells that did not arrive
# via sshd. SSH logins already get /etc/motd from pam_motd, so re-printing
# there would double the banner ($SSH_CONNECTION guard). Non-interactive
# login shells (bash -lc launchers, su - ... -c services) and shells without
# a terminal on stdout are skipped so their output stays unchanged.
case "$-" in
  *i*)
    if [ -z "$SSH_CONNECTION" ] && [ -t 1 ] && [ -r /etc/motd ]; then
      cat /etc/motd
    fi
    ;;
esac
