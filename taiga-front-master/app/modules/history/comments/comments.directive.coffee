###
# Copyright (C) 2014-2018 Taiga Agile LLC
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
# File: history/comments/comments.directive.coffee
###

module = angular.module('taigaHistory')

CommentsDirective = () ->
    link = (scope, el, attrs, ctrl) ->
        ctrl.initializePermissions()

    return {
        scope: {
            type: "<",
            name: "@",
            object: "@",
            comments: "<",
            onEditMode: "&",
            onDeleteComment: "&",
            onRestoreDeletedComment: "&",
            onAddComment: "&",
            onEditComment: "&",
            editMode: "<",
            loading: "<",
            deleting: "<",
            editing: "<",
            project: "=",
            reverse: "="
        },
        templateUrl:"history/comments/comments.html",
        bindToController: true,
        controller: 'CommentsCtrl',
        controllerAs: "vm"
        link: link
    }

module.directive("tgComments", CommentsDirective)
