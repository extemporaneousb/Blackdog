;;; blackdog-artifacts.el --- Artifact navigation for Blackdog -*- lexical-binding: t; -*-

;; Copyright (C) 2026

;;; Commentary:

;; Shared helpers for opening prompt/stdout/stderr/diff/result/run artifacts.

;;; Code:

(require 'blackdog-core)

(defconst blackdog-artifact-labels
  '((prompt . "Prompt")
    (stdout . "Stdout")
    (stderr . "Stderr")
    (diff . "Diff")
    (result . "Result")
    (metadata . "Metadata"))
  "Canonical labels for task artifact links.")

(defun blackdog-task-artifact-href (task artifact)
  "Return the artifact HREF for TASK and ARTIFACT.

ARTIFACT is one of `prompt', `stdout', `stderr', `diff', `metadata',
`result', or `run'.

Direct `*_href' fields from TASK take precedence over `links'."
  (or (pcase artifact
        ('prompt (alist-get 'prompt_href task))
        ('stdout (alist-get 'stdout_href task))
        ('stderr (alist-get 'stderr_href task))
        ('diff (alist-get 'diff_href task))
        ('metadata (alist-get 'metadata_href task))
        ('result (alist-get 'latest_result_href task))
        ('run (alist-get 'run_dir_href task))
        (_ nil))
      (blackdog-task--artifact-link-from-label
       task
       (alist-get artifact blackdog-artifact-labels))))

(defun blackdog-task--artifact-link-from-label (task label)
  "Return a link href from TASK for LABEL.

TASK `links' entries are optional and may be nil for older snapshot rows."
  (when-let ((links (alist-get 'links task)))
    (seq-some (lambda (entry)
                (and (string= label (alist-get 'label entry))
                     (alist-get 'href entry)))
              links)))

(defun blackdog-task-artifacts-links (task)
  "Return canonical artifact link rows for TASK.

Each row is an alist with `label' and `href'.
Direct `*_href' fields are merged with `links' and normalized for task
reader action lists."
  (let ((links (or (alist-get 'links task) nil)))
    (dolist (artifact '(prompt stdout stderr diff metadata result))
      (let ((href (blackdog-task-artifact-href task artifact)))
        (when href
          (let ((label (alist-get artifact blackdog-artifact-labels)))
            (setq links
                  (cons (list (cons 'artifact artifact)
                              (cons 'label label)
                              (cons 'href href))
                        (seq-remove (lambda (row)
                                      (string= (alist-get 'label row) label))
                                    links)))))))
    (seq-sort-by (lambda (row) (alist-get 'label row))
                 #'string< links)))

(defun blackdog-task--current-task (&optional root)
  "Return TASK for the current task-view buffer, or prompt for one.

ROOT is used when the caller prompts and should point to the current
Blackdog project."
  (or (and (boundp 'blackdog-task-id)
           blackdog-task-id
           (blackdog-task-by-id blackdog-task-id nil (or root blackdog-buffer-root)))
      (blackdog-read-task "Task: " nil root)))

(defun blackdog-task-open-artifact (artifact &optional task root other-window)
  "Open TASK artifact ARTIFACT in Blackdog ROOT.

TASK may be an alist, a task ID string, or nil to use the active
task-view context.  ARTIFACT should match the symbols recognized by
`blackdog-task-artifact-href'."
  (let* ((root (or root (blackdog-project-root)))
         (resolved-task (cond
                         ((null task) (blackdog-task--current-task root))
                         ((stringp task)
                          (blackdog-task-by-id task nil root))
                         (t task)))
         (href (blackdog-task-artifact-href resolved-task artifact)))
    (unless resolved-task
      (user-error "Task not found"))
    (unless href
      (user-error "No %s artifact for task %s" artifact (alist-get 'id resolved-task)))
    (blackdog-open-href href nil root other-window)))

(defun blackdog-task-open-prompt (&optional task)
  "Open TASK prompt artifact."
  (interactive)
  (blackdog-task-open-artifact 'prompt task nil t))

(defun blackdog-task-open-stdout (&optional task)
  "Open TASK stdout artifact."
  (interactive)
  (blackdog-task-open-artifact 'stdout task nil t))

(defun blackdog-task-open-stderr (&optional task)
  "Open TASK stderr artifact."
  (interactive)
  (blackdog-task-open-artifact 'stderr task nil t))

(defun blackdog-task-open-diff (&optional task)
  "Open TASK diff artifact."
  (interactive)
  (blackdog-task-open-artifact 'diff task nil t))

(defun blackdog-task-open-metadata (&optional task)
  "Open TASK metadata artifact."
  (interactive)
  (blackdog-task-open-artifact 'metadata task nil t))

(defun blackdog-task-open-result (&optional task)
  "Open TASK result artifact."
  (interactive)
  (blackdog-task-open-artifact 'result task nil t))

(defun blackdog-task-open-run (&optional task)
  "Open TASK run directory artifact."
  (interactive)
  (blackdog-task-open-artifact 'run task nil t))

(provide 'blackdog-artifacts)

;;; blackdog-artifacts.el ends here
