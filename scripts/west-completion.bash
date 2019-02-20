# Bash auto-completion for west subcommands and flags. To initialize, run
#
#     source west-completion.bash
#
# To make it persistent, add it to e.g. your .bashrc.

__comp_west()
{
	# Reset to default, to make sure compgen works properly
	local IFS=$' \t\n'

	# Common arguments for all commands
	local common="--zephyr-base"

	# Common arguments for runners
	local run_common="
	--build-dir
	--cmake-cache
	--runner
	--skip-rebuild
	--board-dir
	--elf-file
	--hex-file
	--bin-file
	--gdb
	--openocd
	--openocd-search"

	# Associative array with flags for subcommands
	local -A flags
	flags[init]="$common --manifest-url --manifest-rev --local"
	flags[help]="$common"
	flags[list]="$common --format"
	flags[manifest]="$common --freeze"
	flags[diff]="$common"
	flags[status]="$common"
	flags[update]="$common --no-update --keep-descendants --rebase"
	flags[selfupdate]="$common --keep-descendants --rebase"
	flags[forall]="$common -c"
	# TODO:these should be moved to the zephyr repository
	flags[build]="$common --board --source-dir --build-dir --target --cmake --force"
	flags[sign]="$common --build-dir --force --tool-path --bin --no-bin --sbin --hex --no-hex --shex"
	flags[flash]="$common $run_common"
	flags[debug]="$common $run_common"
	flags[debugserver]="$common $run_common"
	flags[attach]="$common $run_common"

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
