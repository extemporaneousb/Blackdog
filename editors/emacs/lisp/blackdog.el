;;; blackdog.el --- Emacs workbench for Blackdog -*- lexical-binding: t; -*-

;; Copyright (C) 2026

;; Author: Blackdog contributors
;; Keywords: tools, vc

;;; Commentary:

;; Main entrypoint for the Blackdog Emacs package.

;;; Code:

(require 'blackdog-core)
(require 'blackdog-results)
(require 'blackdog-task)
(require 'blackdog-runs)
(require 'blackdog-artifacts)
(require 'blackdog-magit)
(require 'blackdog-search)
(require 'blackdog-codex)
(require 'blackdog-thread)
(require 'blackdog-spec)
(require 'blackdog-telemetry)

(defvar blackdog-prefix-map
  (let ((map (make-sparse-keymap)))
    (define-key map (kbd "b") #'blackdog-dashboard)
    (define-key map (kbd "c") #'blackdog-claim-task)
    (define-key map (kbd "w") #'blackdog-launch-task)
    (define-key map (kbd "l") #'blackdog-release-task)
    (define-key map (kbd "e") #'blackdog-complete-task)
    (define-key map (kbd "k") #'blackdog-remove-task)
    (define-key map (kbd "x") #'blackdog-start-supervisor)
    (define-key map (kbd "X") #'blackdog-stop-supervisor)
    (define-key map (kbd "u") #'blackdog-runs-open)
    (define-key map (kbd "r") #'blackdog-results-open)
    (define-key map (kbd "t") #'blackdog-find-task)
    (define-key map (kbd "T") #'blackdog-find-codex-session)
    (define-key map (kbd "h") #'blackdog-codex-sessions-open)
    (define-key map (kbd "H") #'blackdog-threads-open)
    (define-key map (kbd "a") #'blackdog-find-artifact)
    (define-key map (kbd "n") #'blackdog-codex-compose-new)
    (define-key map (kbd "N") #'blackdog-spec-new)
    (define-key map (kbd "v") #'blackdog-telemetry-open)
    (define-key map (kbd "f") #'blackdog-find-project-file)
    (define-key map (kbd "s") #'blackdog-search-project)
    (define-key map (kbd "A") #'blackdog-search-artifacts)
    (define-key map (kbd "m") #'blackdog-magit-status-for-task)
    (define-key map (kbd "d") #'blackdog-magit-diff-for-task)
    (define-key map (kbd ".") #'blackdog-dispatch)
    (define-key map (kbd "?") #'blackdog-dispatch)
    (define-key map (kbd "g") #'blackdog-refresh)
    map)
  "Prefix keymap for Blackdog commands.")

(defun blackdog-dashboard ()
  "Open the Blackdog dashboard."
  (interactive)
  (require 'blackdog-dashboard)
  (blackdog-dashboard-open))

(defun blackdog-magit-status-for-task ()
  "Prompt for one task and open Magit status for it."
  (interactive)
  (blackdog-magit-status-task (blackdog-read-task)))

(defun blackdog-magit-diff-for-task ()
  "Prompt for one task and open a branch diff for it."
  (interactive)
  (blackdog-magit-diff-task (blackdog-read-task)))

(defun blackdog-claim-task ()
  "Prompt for one task and claim it."
  (interactive)
  (blackdog-task-claim (blackdog-read-task)))

(defun blackdog-launch-task ()
  "Prompt for one task and launch its WTAM worktree."
  (interactive)
  (blackdog-task-launch (blackdog-read-task)))

(defun blackdog-release-task ()
  "Prompt for one task and release it."
  (interactive)
  (blackdog-task-release (blackdog-read-task)))

(defun blackdog-complete-task ()
  "Prompt for one task and complete it."
  (interactive)
  (blackdog-task-complete (blackdog-read-task)))

(defun blackdog-remove-task ()
  "Prompt for one task and remove it."
  (interactive)
  (blackdog-task-remove (blackdog-read-task)))

(defun blackdog-start-supervisor ()
  "Open the telemetry monitor and start one supervisor run."
  (interactive)
  (blackdog-telemetry-start-supervisor))

(defun blackdog-stop-supervisor ()
  "Request a stop for the active supervisor actor."
  (interactive)
  (blackdog-telemetry-stop-supervisor))

(if (require 'transient nil t)
    (eval
     '(transient-define-prefix blackdog-dispatch ()
        "Dispatch Blackdog commands."
        [["Views"
          ("b" "Dashboard" blackdog-dashboard)
          ("h" "Codex sessions" blackdog-codex-sessions-open)
          ("H" "Blackdog threads" blackdog-threads-open)
          ("u" "Runs" blackdog-runs-open)
          ("r" "Results" blackdog-results-open)
          ("t" "Task" blackdog-find-task)
          ("T" "Codex session" blackdog-find-codex-session)
          ("a" "Artifact" blackdog-find-artifact)
          ("n" "New Codex session" blackdog-codex-compose-new)
          ("N" "New spec" blackdog-spec-new)
          ("v" "Telemetry" blackdog-telemetry-open)
          ("f" "Project file" blackdog-find-project-file)]
         ["Write"
          ("c" "Claim" blackdog-claim-task)
          ("w" "Launch worktree" blackdog-launch-task)
          ("l" "Release" blackdog-release-task)
          ("e" "Complete" blackdog-complete-task)
          ("k" "Remove task" blackdog-remove-task)]
         ["Supervisor"
          ("v" "Telemetry" blackdog-telemetry-open)
          ("x" "Start supervisor" blackdog-start-supervisor)
          ("X" "Stop supervisor" blackdog-stop-supervisor)
          ("u" "Runs" blackdog-runs-open)]
         ["Search"
          ("s" "Project grep" blackdog-search-project)
          ("A" "Artifact grep" blackdog-search-artifacts)
          ("g" "Refresh" blackdog-refresh)]
         ["Git"
          ("m" "Magit status" blackdog-magit-status-for-task)
          ("d" "Magit diff" blackdog-magit-diff-for-task)]]))
  (defun blackdog-dispatch ()
    "Fallback command when Transient is unavailable."
    (interactive)
    (user-error "Transient is required for `blackdog-dispatch'")))

(provide 'blackdog)

;;; blackdog.el ends here
