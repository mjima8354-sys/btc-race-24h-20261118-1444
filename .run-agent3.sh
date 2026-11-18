#!/bin/bash
export OPENROUTER_API_KEY=$(cat /root/secrets/openrouter.key)
export PATH=$PATH:/root/.opencode/bin
/root/.opencode/bin/opencode run -s ses_e10971863ffeBIfrD2LBnVB2UH -m opencode/nemotron-3.5-lightning-free --dir /root/makefun/build/btc-race-24h-20261118-1444 "$(cat /root/makefun/build/btc-race-24h-20261118-1444/.agent-instr3.md)"
