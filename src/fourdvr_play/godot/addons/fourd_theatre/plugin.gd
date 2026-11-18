@tool
extends EditorPlugin
## Adds the autoload "FourD" (this game's connection to the theatre's hub) and its project settings.

const AUTOLOAD := "FourD"
const SETTINGS := {
	"fourd_theatre/url": "ws://127.0.0.1:47020/ws",
	"fourd_theatre/role": "game",
	"fourd_theatre/seat": "",
	"fourd_theatre/key": "",
	"fourd_theatre/auto_connect": true,
}


func _enter_tree() -> void:
	for setting in SETTINGS:
		if not ProjectSettings.has_setting(setting):
			ProjectSettings.set_setting(setting, SETTINGS[setting])
		ProjectSettings.set_initial_value(setting, SETTINGS[setting])


func _enable_plugin() -> void:
	add_autoload_singleton(AUTOLOAD, "res://addons/fourd_theatre/fourd.gd")


func _disable_plugin() -> void:
	remove_autoload_singleton(AUTOLOAD)
