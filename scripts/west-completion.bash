# Bash auto-completion for west subcommands and flags. To initialize, run
#
#     source west-completion.bash
#
# To make it persistent, add it to e.g. your .bashrc.

__comp_west()
{
	# Reset to default, to make sure compgen works properly
	local IFS=$' \t\n'

	# Common arguments for runners
	local run_common="
	--context
	--build-dir
	--cmake-cache
	--runner
	--skip-rebuild
	--board-dir
	--kernel-elf
	--kernel-hex
	--kernel-bin
	--gdb
	--openocd
	--openocd-search"

	# Associative array with flags for subcommands
	local -A flags
	flags[init]="--base-url --manifest-url --manifest-rev --west-url --west-rev"
	flags[build]="--board --source-dir --build-dir --target --cmake --force"
	flags[flash]="$run_common"
	flags[debug]="$run_common"
	flags[debugserver]="$run_common"
	flags[attach]="$run_common"
	flags[list]="--manifest"
	flags[clone]="-b --no-update"
	flags[fetch]="--manifest --no-update"
	flags[pull]="--manifest --no-update"
	flags[rebase]="--manifest"
	flags[branch]="--manifest"
	flags[checkout]="--manifest -b"
	flags[diff]="--manifest"
	flags[status]="--manifest"
	flags[update]="--manifest --update-west --update-manifest"
	flags[forall]="--manifest -c"

	# Word before current location and at current location
	local prev=${COMP_WORDS[COMP_CWORD-1]}
	local cur=${COMP_WORDS[COMP_CWORD]}

	case $COMP_CWORD in
	1)
		case $cur in
		-*)
			# west flag completion
			__set_comp $cur --help --zephyr-base --verbose --version
			;;
		*)
			# west subcommand name completion, using the keys from
			# 'flags'
			__set_comp "$cur" ${!flags[*]}
			;;
		esac
		;;
	2)
		case $cur in
		-*)
			# west subcommand flag completion, using the values
			# from 'flags'
			__set_comp $cur --help ${flags[$prev]}
			;;
		esac
		;;
	esac
}

# Sets completions for $1, from the possibilities in $2..n
__set_comp()
{
	# "${*:2}" gives a single argument with arguments $2..n
	COMPREPLY=($(compgen -W "${*:2}" -- $1))
}

complete -F __comp_west west
