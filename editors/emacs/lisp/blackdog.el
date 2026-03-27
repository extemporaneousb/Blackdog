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

(ignore-errors
  (require 'transient))

(defvar blackdog-prefix-map
  (let ((map (make-sparse-keymap)))
    (define-key map (kbd "b") #'blackdog-dashboard)
    (define-key map (kbd "u") #'blackdog-runs-open)
    (define-key map (kbd "r") #'blackdog-results-open)
    (define-key map (kbd "t") #'blackdog-find-task)
    (define-key map (kbd "a") #'blackdog-find-artifact)
    (define-key map (kbd "f") #'blackdog-find-project-file)
    (define-key map (kbd "s") #'blackdog-search-project)
    (define-key map (kbd "A") #'blackdog-search-artifacts)
    (define-key map (kbd "m") #'blackdog-magit-status-for-task)
    (define-key map (kbd "d") #'blackdog-magit-diff-for-task)
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

(if (featurep 'transient)
    (transient-define-prefix blackdog-dispatch ()
      "Dispatch Blackdog commands."
      [["Views"
        ("b" "Dashboard" blackdog-dashboard)
        ("u" "Runs" blackdog-runs-open)
        ("r" "Results" blackdog-results-open)
        ("t" "Task" blackdog-find-task)
        ("a" "Artifact" blackdog-find-artifact)
        ("f" "Project file" blackdog-find-project-file)]
       ["Search"
        ("s" "Project grep" blackdog-search-project)
        ("A" "Artifact grep" blackdog-search-artifacts)
        ("g" "Refresh" blackdog-refresh)]
       ["Git"
        ("m" "Magit status" blackdog-magit-status-for-task)
        ("d" "Magit diff" blackdog-magit-diff-for-task)]])
  (defun blackdog-dispatch ()
    "Fallback command when Transient is unavailable."
    (interactive)
    (user-error "Transient is required for `blackdog-dispatch'")))

(provide 'blackdog)

;;; blackdog.el ends here
