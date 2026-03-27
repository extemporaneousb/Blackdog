;;; blackdog-core.el --- Core helpers for Blackdog Emacs integration -*- lexical-binding: t; -*-

;; Copyright (C) 2026

;; Author: Blackdog contributors
;; Keywords: tools, vc

;;; Commentary:

;; Core process, JSON, cache, and path helpers for the Blackdog Emacs package.

;;; Code:

(require 'json)
(require 'seq)
(require 'subr-x)

(defgroup blackdog nil
  "Operate Blackdog from Emacs."
  :group 'tools
  :prefix "blackdog-")

(defcustom blackdog-default-command nil
  "Absolute path to the `blackdog' CLI.

When nil, resolve `./.VE/bin/blackdog' from the current project root and
fall back to `blackdog' on PATH."
  :type '(choice (const :tag "Auto-detect" nil) file)
  :group 'blackdog)

(defvar blackdog--snapshot-cache (make-hash-table :test #'equal))
(defvar-local blackdog-buffer-root nil)
(defvar-local blackdog-refresh-function nil)

(defun blackdog-project-root (&optional dir)
  "Return the Blackdog project root above DIR or `default-directory'."
  (let ((root (locate-dominating-file (or dir default-directory) "blackdog.toml")))
    (unless root
      (user-error "No blackdog.toml found above %s" (or dir default-directory)))
    (directory-file-name (expand-file-name root))))

(defun blackdog-command (&optional root)
  "Return the Blackdog CLI path for ROOT."
  (or blackdog-default-command
      (let ((candidate (expand-file-name ".VE/bin/blackdog"
                                         (or root (blackdog-project-root)))))
        (cond
         ((file-executable-p candidate) candidate)
         ((executable-find "blackdog"))
         (t (user-error "Could not find a Blackdog CLI for %s"
                        (or root (blackdog-project-root))))))))

(defun blackdog--call (root &rest args)
  "Run Blackdog in ROOT with ARGS and return stdout."
  (let ((default-directory root)
        (buffer (generate-new-buffer " *blackdog*")))
    (unwind-protect
        (let ((status (apply #'process-file (blackdog-command root) nil buffer nil args)))
          (with-current-buffer buffer
            (let ((output (buffer-string)))
              (if (zerop status)
                  output
                (error "blackdog %s failed (%s): %s"
                       (string-join args " ")
                       status
                       (string-trim output))))))
      (kill-buffer buffer))))

(defun blackdog--call-json (root &rest args)
  "Run Blackdog in ROOT with ARGS and parse the JSON response."
  (let ((json-object-type 'alist)
        (json-array-type 'list)
        (json-key-type 'symbol)
        (json-false :json-false)
        (json-null nil))
    (json-read-from-string
     (string-trim (apply #'blackdog--call root args)))))

(defun blackdog-clear-cache (&optional root)
  "Clear the cached snapshot for ROOT, or every cached snapshot when ROOT is nil."
  (if root
      (remhash (expand-file-name root) blackdog--snapshot-cache)
    (clrhash blackdog--snapshot-cache)))

(defun blackdog-snapshot (&optional root force)
  "Return the current Blackdog snapshot for ROOT.

When FORCE is non-nil, refresh the cached snapshot."
  (let* ((root (expand-file-name (or root (blackdog-project-root))))
         (cached (and (not force) (gethash root blackdog--snapshot-cache))))
    (or cached
        (let ((snapshot (blackdog--call-json root "snapshot")))
          (puthash root snapshot blackdog--snapshot-cache)
          snapshot))))

(defun blackdog-control-dir (&optional snapshot root)
  "Return the Blackdog control dir from SNAPSHOT or ROOT."
  (alist-get 'control_dir (or snapshot (blackdog-snapshot root))))

(defun blackdog-task-by-id (task-id &optional snapshot root)
  "Return TASK-ID from SNAPSHOT or ROOT."
  (seq-find (lambda (task)
              (equal task-id (alist-get 'id task)))
            (alist-get 'tasks (or snapshot (blackdog-snapshot root)))))

(defun blackdog-task-candidates (&optional snapshot root)
  "Return completion candidates for tasks from SNAPSHOT or ROOT."
  (mapcar
   (lambda (task)
     (cons (format "[%s] %s %s"
                   (or (alist-get 'operator_status task)
                       (alist-get 'status task)
                       "?")
                   (alist-get 'id task)
                   (alist-get 'title task))
           task))
   (alist-get 'tasks (or snapshot (blackdog-snapshot root)))))

(defun blackdog-read-task (&optional prompt snapshot root)
  "Read one task from SNAPSHOT or ROOT with PROMPT."
  (let* ((snapshot (or snapshot (blackdog-snapshot root)))
         (candidates (blackdog-task-candidates snapshot))
         (choice (completing-read (or prompt "Task: ") candidates nil t)))
    (cdr (assoc choice candidates))))

(defun blackdog-resolve-href (href &optional snapshot root)
  "Resolve relative HREF against the control dir from SNAPSHOT or ROOT."
  (when (and href (not (string-empty-p href)))
    (cond
     ((string-match-p "\\`https?://" href) href)
     ((file-name-absolute-p href) href)
     (t (expand-file-name href (blackdog-control-dir snapshot root))))))

(defun blackdog-resolve-project-path (path &optional root)
  "Resolve PATH relative to ROOT."
  (when (and path (not (string-empty-p path)))
    (if (file-name-absolute-p path)
        path
      (expand-file-name path (or root (blackdog-project-root))))))

(defun blackdog-open-href (href &optional snapshot root other-window)
  "Open HREF using SNAPSHOT or ROOT.

When OTHER-WINDOW is non-nil, use another window for file paths."
  (let ((target (blackdog-resolve-href href snapshot root)))
    (unless target
      (user-error "No artifact path available"))
    (if (string-match-p "\\`https?://" target)
        (browse-url target)
      (funcall (if other-window #'find-file-other-window #'find-file) target))))

(defun blackdog-open-project-path (path &optional root other-window)
  "Open project PATH relative to ROOT.

When OTHER-WINDOW is non-nil, use another window."
  (let ((target (blackdog-resolve-project-path path root)))
    (unless target
      (user-error "No project path available"))
    (funcall (if other-window #'find-file-other-window #'find-file) target)))

(defun blackdog-refresh (&optional root)
  "Refresh the current Blackdog buffer and clear cached data for ROOT."
  (interactive)
  (let ((root (or root blackdog-buffer-root (blackdog-project-root))))
    (blackdog-clear-cache root)
    (when (functionp blackdog-refresh-function)
      (funcall blackdog-refresh-function))))

(provide 'blackdog-core)

;;; blackdog-core.el ends here
